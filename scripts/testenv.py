"""Local ClickHouse test sources owned by this PoC (loopback only).

Topology
  cluster_a : a1, a2  (TLS native port, self-signed CA, 2 shards)
  cluster_b : b1      (plaintext native port; isolated local test only)

Every node has three users. Passwords are random and stored only in
var/testenv/secrets.json (0600, gitignored):
  admin          full access, used by the workload generator
  mlh_collector  SELECT on system.metrics / system.query_log / system.clusters
  mlh_noperm     SELECT on system.one only (permission-denied scenario)

Commands
  init                 create CA/cert, configs, secrets (idempotent)
  start [node...]      start nodes (default: all)
  stop  [node...]      stop nodes (default: all)
  status               show pid / port reachability
  workload <tag> [n]   run n tagged queries on every running node via HTTP
  sql <node> <query>   run one query as admin via HTTP (prints TSV)
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / ".tools" / "clickhouse"
BASE = ROOT / "var" / "testenv"
CERTS = BASE / "certs"
SECRETS_FILE = BASE / "secrets.json"
HOST = "127.0.0.1"


@dataclass(frozen=True)
class Node:
    name: str
    cluster: str
    http_port: int
    tcp_port: int
    tcp_secure_port: int | None

    @property
    def native_port(self) -> int:
        return self.tcp_secure_port or self.tcp_port

    @property
    def tls(self) -> bool:
        return self.tcp_secure_port is not None

    @property
    def dir(self) -> Path:
        return BASE / self.name


NODES = {
    "a1": Node("a1", "cluster_a", 18001, 19001, 19401),
    "a2": Node("a2", "cluster_a", 18002, 19002, 19402),
    "b1": Node("b1", "cluster_b", 18003, 19003, None),
}


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def ensure_certs() -> None:
    CERTS.mkdir(parents=True, exist_ok=True)
    ca_key, ca_pem = CERTS / "ca.key", CERTS / "ca.pem"
    srv_key, srv_csr, srv_pem = CERTS / "server.key", CERTS / "server.csr", CERTS / "server.pem"
    ext = CERTS / "server.ext"
    if srv_pem.exists():
        return
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
         "-subj", "/CN=mini-loghouse-test-ca", "-keyout", str(ca_key), "-out", str(ca_pem)])
    ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
    run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
         "-keyout", str(srv_key), "-out", str(srv_csr)])
    run(["openssl", "x509", "-req", "-in", str(srv_csr), "-CA", str(ca_pem), "-CAkey", str(ca_key),
         "-CAcreateserial", "-days", "30", "-extfile", str(ext), "-out", str(srv_pem)])
    for key in (ca_key, srv_key):
        key.chmod(0o600)


def load_secrets() -> dict[str, str]:
    return json.loads(SECRETS_FILE.read_text())


def ensure_secrets() -> dict[str, str]:
    if SECRETS_FILE.exists():
        return load_secrets()
    values = {user: secrets.token_urlsafe(18) for user in ("admin", "mlh_collector", "mlh_noperm")}
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SECRETS_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(values, fh)
    return values


def remote_servers_xml(own_cluster: str) -> str:
    """Each node only knows its own cluster, like separately operated clusters."""
    clusters: dict[str, list[Node]] = {}
    for node in NODES.values():
        clusters.setdefault(node.cluster, []).append(node)
    parts = []
    for cluster, nodes in clusters.items():
        if cluster != own_cluster:
            continue
        shards = "".join(
            f"<shard><replica><host>{HOST}</host><port>{n.native_port}</port>"
            f"<secure>{1 if n.tls else 0}</secure><user>admin</user>"
            f"<password from_env=\"MLH_TEST_ADMIN_PASSWORD\"/></replica></shard>"
            for n in nodes
        )
        parts.append(f"<{cluster}>{shards}</{cluster}>")
    return "<remote_servers>" + "".join(parts) + "</remote_servers>"


def config_xml(node: Node) -> str:
    d = node.dir
    secure_port = f"<tcp_port_secure>{node.tcp_secure_port}</tcp_port_secure>" if node.tls else ""
    return f"""<clickhouse>
    <logger><level>information</level><log>{d}/log/server.log</log><errorlog>{d}/log/error.log</errorlog><size>20M</size><count>2</count></logger>
    <display_name>{node.name}</display_name>
    <listen_host>{HOST}</listen_host>
    <http_port>{node.http_port}</http_port>
    <tcp_port>{node.tcp_port}</tcp_port>
    {secure_port}
    <openSSL>
        <server>
            <certificateFile>{CERTS}/server.pem</certificateFile>
            <privateKeyFile>{CERTS}/server.key</privateKeyFile>
            <verificationMode>none</verificationMode>
            <loadDefaultCAFile>false</loadDefaultCAFile>
        </server>
        <client>
            <caConfig>{CERTS}/ca.pem</caConfig>
            <verificationMode>strict</verificationMode>
            <loadDefaultCAFile>false</loadDefaultCAFile>
        </client>
    </openSSL>
    <path>{d}/data/</path>
    <tmp_path>{d}/tmp/</tmp_path>
    <user_files_path>{d}/user_files/</user_files_path>
    <default_profile>default</default_profile>
    <default_database>default</default_database>
    <mlock_executable>false</mlock_executable>
    <max_server_memory_usage>1073741824</max_server_memory_usage>
    <mark_cache_size>134217728</mark_cache_size>
    <query_log>
        <database>system</database>
        <table>query_log</table>
        <flush_interval_milliseconds>7500</flush_interval_milliseconds>
    </query_log>
    {remote_servers_xml(node.cluster)}
    <user_directories>
        <users_xml><path>{d}/users.xml</path></users_xml>
        <local_directory><path>{d}/access/</path></local_directory>
    </user_directories>
</clickhouse>
"""


def users_xml() -> str:
    return """<clickhouse>
    <profiles><default><log_queries>1</log_queries></default></profiles>
    <quotas><default/></quotas>
    <users>
        <admin>
            <password from_env="MLH_TEST_ADMIN_PASSWORD"/>
            <networks><ip>127.0.0.1</ip></networks>
            <profile>default</profile><quota>default</quota>
            <access_management>1</access_management>
        </admin>
        <mlh_collector>
            <password from_env="MLH_TEST_COLLECTOR_PASSWORD"/>
            <networks><ip>127.0.0.1</ip></networks>
            <profile>default</profile><quota>default</quota>
            <grants>
                <query>GRANT SELECT ON system.metrics</query>
                <query>GRANT SELECT ON system.query_log</query>
                <query>GRANT SELECT ON system.clusters</query>
                <query>GRANT SELECT ON system.one</query>
            </grants>
        </mlh_collector>
        <mlh_noperm>
            <password from_env="MLH_TEST_NOPERM_PASSWORD"/>
            <networks><ip>127.0.0.1</ip></networks>
            <profile>default</profile><quota>default</quota>
            <grants><query>GRANT SELECT ON system.one</query></grants>
        </mlh_noperm>
    </users>
</clickhouse>
"""


def init() -> None:
    ensure_certs()
    ensure_secrets()
    for node in NODES.values():
        for sub in ("data", "tmp", "user_files", "log"):
            (node.dir / sub).mkdir(parents=True, exist_ok=True)
        (node.dir / "config.xml").write_text(config_xml(node))
        (node.dir / "users.xml").write_text(users_xml())
    os.chmod(BASE, 0o700)
    print(f"initialized {', '.join(NODES)} under {BASE}")


def node_env() -> dict[str, str]:
    values = load_secrets()
    return {
        **os.environ,
        "MLH_TEST_ADMIN_PASSWORD": values["admin"],
        "MLH_TEST_COLLECTOR_PASSWORD": values["mlh_collector"],
        "MLH_TEST_NOPERM_PASSWORD": values["mlh_noperm"],
    }


def pid_of(node: Node) -> int | None:
    pid_file = node.dir / "server.pid"
    if not pid_file.exists():
        return None
    pid = int(pid_file.read_text().strip() or 0)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) == 0


def start(names: list[str]) -> None:
    env = node_env()
    for node in (NODES[n] for n in names):
        if pid_of(node):
            print(f"{node.name}: already running")
            continue
        out = open(node.dir / "log" / "stdout.log", "ab")
        proc = subprocess.Popen(
            [str(BINARY), "server", f"--config-file={node.dir / 'config.xml'}"],
            cwd=node.dir, env=env, stdout=out, stderr=out, start_new_session=True,
        )
        (node.dir / "server.pid").write_text(str(proc.pid))
    for node in (NODES[n] for n in names):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not port_open(node.http_port):
            time.sleep(0.5)
        print(f"{node.name}: {'up' if port_open(node.http_port) else 'NOT UP'} pid={pid_of(node)}")


def stop(names: list[str]) -> None:
    for node in (NODES[n] for n in names):
        pid = pid_of(node)
        if not pid:
            print(f"{node.name}: not running")
            continue
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and pid_of(node):
            time.sleep(0.5)
        if pid_of(node):
            os.kill(pid, signal.SIGKILL)
        print(f"{node.name}: stopped")


def status() -> None:
    for node in NODES.values():
        ports = {"http": node.http_port, "native": node.native_port}
        reach = {k: port_open(v) for k, v in ports.items()}
        print(f"{node.name} cluster={node.cluster} tls={node.tls} pid={pid_of(node)} {ports} reachable={reach}")


def http_query(node: Node, query: str, query_id: str | None = None, timeout: float = 10) -> str:
    params = {"query": query, "log_comment": "mlh-testenv"}
    if query_id:
        params["query_id"] = query_id
    url = f"http://{HOST}:{node.http_port}/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="POST")
    req.add_header("X-ClickHouse-User", "admin")
    req.add_header("X-ClickHouse-Key", load_secrets()["admin"])
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback test source
        return resp.read().decode()


def workload(tag: str, count: int) -> None:
    """Run `count` OK queries + 1 failing query per running node, with unique query_ids."""
    issued: list[dict] = []
    for node in NODES.values():
        if not pid_of(node):
            continue
        for i in range(count):
            qid = f"{tag}-{node.name}-ok-{i}"
            http_query(node, f"SELECT sum(number) FROM numbers({100000 * (i + 1)})", qid)
            issued.append({"node": node.name, "query_id": qid, "kind": "ok"})
        qid = f"{tag}-{node.name}-err-0"
        try:
            http_query(node, "SELECT throwIf(1, 'mlh test failure')", qid)
        except urllib.error.HTTPError:
            pass
        issued.append({"node": node.name, "query_id": qid, "kind": "error"})
    print(json.dumps(issued))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, rest = argv[1], argv[2:]
    if cmd == "init":
        init()
    elif cmd == "start":
        start(rest or list(NODES))
    elif cmd == "stop":
        stop(rest or list(NODES))
    elif cmd == "status":
        status()
    elif cmd == "workload":
        workload(rest[0], int(rest[1]) if len(rest) > 1 else 5)
    elif cmd == "sql":
        print(http_query(NODES[rest[0]], rest[1]), end="")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
