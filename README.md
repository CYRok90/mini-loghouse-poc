# chDB Mini LogHouse PoC

웹에서 ClickHouse 클러스터(endpoint·계정·password)를 등록하면, backend 안의 **chDB Refreshable Materialized View(RMV)가 `remote()`/`remoteSecure()`로 각 노드의 `system.metrics`·`system.query_log`를 주기적으로 가져와 chDB에 저장**하고, 목록·상세 Dashboard가 그 저장 데이터만 조회하는 PoC입니다.

> **범위**: 로컬(loopback) 단일 운영자용 PoC입니다. 다중 사용자 인증·인가가 없으므로 외부에 공개할 수 있는 제품이 아닙니다.
> 검증 결과와 한계는 [`reports/feasibility.md`](reports/feasibility.md)에 있습니다.

## 구성

```
브라우저 ──(same-origin JSON API)──▶ FastAPI backend (127.0.0.1, 단일 프로세스·단일 worker)
                                        ├─ ChdbOwner: flock + chDB Session 1개 + 직렬 executor
                                        │    └─ chDB (var/app/chdb)
                                        │        ├─ named collection mlh_<node>  (자격증명, local_encrypted)
                                        │        ├─ RMV mv_m_<node> ─ REFRESH EVERY R APPEND ─▶ telemetry.metrics_raw
                                        │        └─ RMV mv_q_<node> ─ REFRESH EVERY R APPEND ─▶ telemetry.query_log_raw (ReplacingMergeTree)
                                        ├─ Supervisor thread: view_refreshes 관찰 → 상태·cursor·gap(SQLite), gap backfill
                                        ├─ probe subprocess: 연결·권한 검사 (in-memory chDB, password는 stdin)
                                        └─ SQLite registry (var/app/registry.sqlite3): 등록 정보·상태·cursor·gap (password 없음)
```

| 선택 | 이유 |
|---|---|
| Python 3.11 + FastAPI/uvicorn | 새 프로젝트이므로 유지보수가 단순한 Python backend. chDB Python API를 그대로 사용 |
| vanilla HTML/JS + 자체 SVG 차트 | 빌드 단계·외부 CDN 없음, CSP `script-src 'self'` 유지 |
| SQLite registry | 등록 metadata는 최소 구성. chDB가 열리지 않는 복구 모드에서도 상태·이력 조회 가능 |
| chDB named collection(암호화) | password를 RMV DDL·metadata에 넣지 않고, 재시작 후 재입력 없이 수집 재개 |

## 요구 사항

- macOS arm64 또는 Linux, Python 3.11, [uv](https://docs.astral.sh/uv/)
- 설치 후 디스크 약 1.5 GB (venv 0.6 GB, 테스트용 ClickHouse 바이너리 0.85 GB)
- 고정 dependency: `chdb==4.4.0`, `chdb-core==26.9.0`(내장 engine 26.9.2.1), `fastapi==0.162.0`, `uvicorn==0.54.0` (`uv.lock`)

## 설치

```bash
scripts/setup.sh          # uv sync + (macOS) chDB Mach-O 정렬 패치 + import 확인
```

macOS 27에서는 공개 chDB wheel이 `mis-aligned LINKEDIT string pool` 오류로 import되지 않습니다. `scripts/fix_macho_strtab_alignment.py`가 venv 안의 사본만 8바이트 정렬하고 ad-hoc 재서명합니다(파일 크기·코드 불변, uv 캐시 미변경). SQL 기능과는 무관한 패키징 문제입니다.

## 로컬 테스트 source (PoC 전용 인스턴스)

ClickHouse 26.8 LTS 단일 바이너리를 `.tools/clickhouse`에 둡니다.

```bash
mkdir -p .tools && curl -L -o .tools/clickhouse \
  https://github.com/ClickHouse/ClickHouse/releases/download/v26.8.34.13-lts/clickhouse-macos-aarch64
chmod +x .tools/clickhouse            # sha256 d1666bb409243aee970c6ea76d8b97e18cbcb6351d61d52dd3d88baab14d26db (다운로드 시점)
.venv/bin/python scripts/testenv.py init     # CA/서버 인증서, 설정, 무작위 password(var/testenv/secrets.json, 0600)
.venv/bin/python scripts/testenv.py start    # a1:19401(TLS) a2:19402(TLS) = cluster_a / b1:19003(평문) = cluster_b
.venv/bin/python scripts/testenv.py status
.venv/bin/python scripts/testenv.py workload <tag> 5   # 노드마다 tag가 붙은 성공 5건 + 실패 1건
```

모든 인스턴스는 `127.0.0.1`에만 bind합니다. 노드마다 `admin`, `mlh_collector`(metrics·query_log·clusters SELECT), `mlh_noperm`(system.one만) 계정이 있습니다. 평문 노드 b1은 격리된 로컬 테스트용입니다.

## 실행 / 종료

```bash
set -a; source scripts/dev.env; set +a     # 테스트 설정(비밀 없음): refresh 5s, lag 8s, overlap 15s
.venv/bin/python -m mlh                    # http://127.0.0.1:8765
# 종료: Ctrl+C (또는 SIGTERM) → supervisor 정지 → chDB close → lock 해제
.venv/bin/python scripts/testenv.py stop
```

기본값(환경변수 없음)은 refresh 15s, safety lag 10s, overlap 30s, 데이터 `var/app`, 키 `~/.config/mini-loghouse/nc.key`입니다.

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| `MLH_DATA_DIR` | `var/app` | chDB·SQLite·lock (0700) |
| `MLH_KEY_FILE` | `~/.config/mini-loghouse/nc.key` | NC 암호화 키. **data dir 밖**, 최초 1회 생성(0600), 자동 재생성 안 함 |
| `MLH_TLS_CA_FILE` | (없음 → 시스템 CA) | remoteSecure 검증용 CA. chDB 전역 설정이라 클러스터별로 다르게 줄 수 없음 |
| `MLH_REFRESH_SECONDS` / `MLH_SAFETY_LAG_SECONDS` / `MLH_OVERLAP_SECONDS` | 15 / 10 / 30 | RMV 주기, flush 여유, 재조회 겹침 |
| `MLH_INITIAL_BACKFILL_SECONDS` / `MLH_MAX_BACKFILL_SECONDS` | 900 / 86400 | 등록 직후 과거 조회, 복구 한도(초과분은 unrecoverable gap) |

두 번째 backend를 같은 data dir로 띄우면 `AlreadyRunning`으로 즉시 종료합니다(중복 RMV 방지). `--reload`·다중 worker로 실행하지 마세요.

## 화면

- `/` 클러스터 목록: 연결 확인 결과(정상·실패·미확인 — 권한/테이블 오류는 연결 실패로 보지 않음), 수집 상태(준비 중·첫 수집 대기·수집 중·수집 지연·수집 오류), 마지막 성공 시각, 노드 수집 범위(전체 검증됨·일부·미확인), 최근 오류
- `/clusters/new` 클러스터 추가: 연결 테스트(접속·인증과 system table 권한을 분리 표시, `system.clusters` 기반 노드 발견) → 저장 및 수집 시작
- `/clusters/<id>` Dashboard: 기간(15분·1시간·24시간)·노드 필터, 자동 갱신, KPI, 쿼리 수·p95 차트, system.metrics, 최근·느린·오류 쿼리 표, 집계 정의, 클러스터 삭제. 선택 기간과 실제 수집 구간의 교집합으로 `미수집`·`부분 집계`·`실제 0`을 구분

## 자격증명 정책

- 브라우저는 backend API만 호출합니다. password는 폼에만 있고 저장 요청 직후 지워지며, 어떤 조회 API도 반환하지 않습니다.
- password는 chDB named collection(`local_encrypted`, AES-128-CTR)에만 저장됩니다. 키는 data dir 밖 파일에 있습니다. RMV DDL과 metadata에는 NC 이름만 남습니다.
- 재시작 후에는 같은 키로 NC가 로드되어 **재입력 없이 수집이 재개**됩니다(검증 완료). 키가 없거나 다르면 앱은 **복구 모드**로 뜹니다. chDB를 열지 않고 SQLite의 등록·상태만 보여 줍니다. 복구 방법은 두 가지입니다. ① 원래 키 파일을 복원합니다. ② 자격증명 초기화: backend를 멈추고 `var/app/chdb/named_collections/`를 다른 곳으로 옮긴 뒤, `sqlite3 var/app/registry.sqlite3 "DELETE FROM meta WHERE key='key_fingerprint'"`를 실행하고 재시작합니다. 노드는 `수집 오류(자격증명 없음)`으로 표시되므로 Dashboard의 `클러스터 삭제`로 지운 뒤 다시 등록합니다. 복구 모드에서는 수집이 멈춘 상태이므로 목록에 `수집 오류(복구 모드)`로 표시됩니다(검증: `experiments/recovery_mode.py`).
- 로그·예외 응답은 분류 코드와 고정 메시지만 내보냅니다. chDB 원본 예외는 SQL(이스케이프된 password 포함)을 되돌려줄 수 있기 때문입니다.
- 한계: 이 보호는 "키 없이 data dir만 유출된 경우"에 대한 것입니다. 같은 OS 사용자 권한을 가진 공격자, 수집된 **SQL 원문(query_log.query)** 자체의 민감정보는 보호 범위 밖입니다.

## 테스트

```bash
.venv/bin/python -m pytest                          # 단위 테스트(오류 분류·cursor/gap·입력 검증·보안 미들웨어)
.venv/bin/python experiments/phase_a_rmv.py create var/x.chdb && .venv/bin/python experiments/phase_a_rmv.py idle var/x.chdb 22
.venv/bin/python experiments/day0_remote_rmv.py probe var/day0.chdb    # MLH_NC_KEY_HEX 필요, testenv 실행 중
.venv/bin/python experiments/phase_c_resilience.py all                 # backend·testenv 실행 + A(19401)/B(19003) 등록 상태, 실패 시 exit 1
.venv/bin/python experiments/recovery_mode.py                          # 별도 data dir·포트 8766에서 키 누락/불일치 복구 모드
```

증거는 `evidence/`에 있습니다(phase_a, phase_a2, day0, app, phase_c, e2e 스크린샷). 브라우저 E2E는 Playwright(MCP)로 수행했습니다.

## 디렉터리

```
mlh/            backend (api, security, chdb_owner, schema, provisioner, supervisor, status, dashboard, probe, registry, ...)
mlh/static/     웹 UI (index/add/cluster.html, *.js, app.css)
experiments/    Phase A·A2·Day0·Phase C 실험 스크립트
scripts/        setup.sh, testenv.py, fix_macho_strtab_alignment.py, dev.env
tests/          단위 테스트
docs/           council brief/결과, Codex 리뷰
reports/        feasibility.md
evidence/       실행 로그·JSON·스크린샷
var/ .tools/    (gitignore) 런타임 데이터, 테스트 인스턴스, 비밀, 바이너리
```
