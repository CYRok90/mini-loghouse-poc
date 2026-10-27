"""Key recovery mode on an isolated data dir / key / port (does not touch var/app).

normal register -> key removed -> restored -> replaced by a different key.
Usage: python experiments/recovery_mode.py   (testenv must be running)
Writes evidence/phase_c/recovery_mode.json
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "var" / "rtest"
KEY = ROOT / "var" / "rtest-key" / "nc.key"
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
HDR = {"Origin": BASE, "X-MLH-Request": "1"}


def env() -> dict[str, str]:
    return {**os.environ, "MLH_DATA_DIR": str(DATA), "MLH_KEY_FILE": str(KEY), "MLH_PORT": str(PORT),
            "MLH_TLS_CA_FILE": str(ROOT / "var/testenv/certs/ca.pem"), "MLH_REFRESH_SECONDS": "5",
            "MLH_OVERLAP_SECONDS": "15", "MLH_SAFETY_LAG_SECONDS": "8", "MLH_INITIAL_BACKFILL_SECONDS": "0"}


def start() -> subprocess.Popen:
    log = open(ROOT / "evidence" / "phase_c" / "recovery-backend.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "mlh"], cwd=ROOT, env=env(), stdout=log, stderr=log)
    for _ in range(120):
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("backend did not come up")


def stop(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=60)


def snapshot(label: str) -> dict:
    c = httpx.Client(base_url=BASE, timeout=30)
    health = c.get("/api/health").json()
    clusters = c.get("/api/clusters").json()
    out = {"label": label, "mode": health["mode"], "recovery_reason": health["recovery_reason"],
           "clusters": [(x["name"], x["state"], [n["phase"] for n in x["nodes"]]) for x in clusters["clusters"]]}
    if clusters["clusters"]:
        cid = clusters["clusters"][0]["id"]
        out["dashboard_status"] = c.get(f"/api/clusters/{cid}/dashboard").status_code
    return out


def main() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(KEY.parent, ignore_errors=True)
    secrets_ = json.loads((ROOT / "var/testenv/secrets.json").read_text())
    steps = []
    proc = start()
    body = {"name": "recovery-b", "host": "127.0.0.1", "port": 19003, "tls": False, "username": "mlh_collector",
            "password": secrets_["mlh_collector"], "nodes": [{"host": "127.0.0.1", "port": 19003}]}
    reg = httpx.post(f"{BASE}/api/clusters", json=body, headers=HDR, timeout=60)
    time.sleep(12)
    steps.append({**snapshot("1 registered"), "register_status": reg.status_code})
    stop(proc)
    KEY.rename(KEY.with_suffix(".moved"))
    proc = start()
    steps.append(snapshot("2 key file missing"))
    stop(proc)
    KEY.with_suffix(".moved").rename(KEY)
    proc = start()
    time.sleep(12)
    steps.append(snapshot("3 key restored"))
    stop(proc)
    original = KEY.read_text()
    KEY.write_text("0" * 32 + "\n")
    proc = start()
    steps.append(snapshot("4 different key"))
    stop(proc)
    KEY.write_text(original)
    proc = start()
    cid = httpx.get(f"{BASE}/api/clusters").json()["clusters"][0]["id"]
    deleted = httpx.delete(f"{BASE}/api/clusters/{cid}", headers=HDR).status_code
    stop(proc)
    result = {"steps": steps, "cleanup_delete_status": deleted}
    (ROOT / "evidence" / "phase_c" / "recovery_mode.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
