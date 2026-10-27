# chDB Mini LogHouse PoC — 기술타당성 보고서

- 작성: 2026-10-27~28 · 대상 명세: `../mini-loghouse-poc-goal.md`
- 범위: 로컬(loopback) 단일 운영자 PoC. 외부 공개 가능한 제품이 아님.

## 1. 결론

| 구분 | 판정 |
|---|---|
| **기술타당성 (원안: 앱 내 chDB + RMV + remote/remoteSecure)** | **PASS (조건부)** — 정상 상태의 주기 수집은 원안 그대로 동작했다. 다만 RMV는 상주 프로세스가 살아 있는 동안만 실행되고 중단 구간을 메우지 않으므로, **다운타임·장애 구간 복구는 앱이 chDB에서 실행하는 명시적 `INSERT … SELECT remote()` backfill(대안 경로)이 담당**한다. |
| **웹 PoC 완료 판정 (명세 9절 6개 조건)** | **충족** — ① 웹에서 테스트 클러스터 2개 등록 ② 실제 연결·필수 권한 검사와 안전한 오류 UX ③ RMV가 주기적으로 실제 데이터를 chDB에 저장 ④ 상태·freshness·수집 범위·실제 Dashboard ⑤ 장애·재시작·중복 검증과 재현 스크립트 ⑥ 브라우저 사용자 흐름 검증 |
| **환경 제약** | macOS 27에서 공개 chDB wheel이 그대로는 import되지 않음(Mach-O 정렬). 패치 후 정상 동작. SQL 기능 문제가 아님 |

책임 분리(실제 실행 경로):

| 역할 | 실행 주체 | 경로 |
|---|---|---|
| system.metrics snapshot 수집 | chDB RMV `mv_m_<node>` | 원안 |
| system.query_log 정상 상태 수집 (고정 재조회 구간) | chDB RMV `mv_q_<node>` | 원안 |
| 수집 구간(cursor)·gap 판정 | 앱 supervisor (view_refreshes 관찰 → SQLite) | 앱 |
| gap backfill (다운타임·노드 장애·등록 직후 과거분) | 앱이 chDB로 실행하는 `INSERT … SELECT remote()` | **대안 경로** |
| 연결·권한 검사 | probe subprocess (in-memory chDB) | 앱 |

## 2. 버전·환경

| 항목 | 값 |
|---|---|
| OS / HW | macOS 27.0.1, arm64 10 core, 16 GB, 가용 디스크 약 9~11 GB(작업 중 사용자 확보) |
| Python / 도구 | Python 3.11.11 (Homebrew), uv 0.10.2 |
| chDB | `chdb==4.4.0`, `chdb-core==26.9.0`, 내장 engine **26.9.2.1** |
| 원격 ClickHouse | 공식 macOS 바이너리 **26.8.34.13 LTS**, 로컬 3 인스턴스 (cluster_a: a1·a2 TLS, cluster_b: b1 평문) |
| Web | FastAPI 0.162.0, uvicorn 0.54.0, pydantic 2.14.1, vanilla JS |
| Docker | 설치되어 있으나 daemon 미기동 → 사용하지 않음 |

환경 이슈와 처리:
1. 디스크 여유가 약 200 MB라 셸 출력조차 ENOSPC로 실패했다 → 기존 파일 삭제 권한이 없어 작업을 중단하고 사용자에게 공간 확보를 요청했다(사용자 확보 후 재개).
2. chdb 4.4.0/core 26.9.0 import가 `mis-aligned LINKEDIT string pool`로 실패했다. 이전 조사(4.0.2/26.1.0)와 같은 증상이다. 원인은 `LC_SYMTAB.stroff`가 4바이트 정렬(8바이트 아님)이라 macOS 27 dyld가 거부한 것이다. 문자열 테이블과 코드서명 사이의 4바이트 여유로 옮기고 ad-hoc 재서명하자 해결됐다(`scripts/fix_macho_strtab_alignment.py`, 파일 크기·코드 불변). **환경 실패로 분류했고, SQL 기능 판단에는 쓰지 않았다.**
3. GitHub에서 받은 ClickHouse 바이너리 실행이 auto mode 권한 분류기에 차단됐다 → 우회하지 않고 사용자 승인을 받은 뒤 실행했다.

## 3. 가설별 결과

| ID | 가설 | 결과 | 근거 |
|---|---|---|---|
| A1 | 영속 Session에서 `REFRESH EVERY … APPEND` RMV가 SQL 없는 유휴 중 반복 실행된다 | **PASS** | 유휴 22초 중 5초 경계 refresh 4회 (`evidence/phase_a/run1.log`) |
| A2 | 종료·재시작 후 데이터·MV 정의가 보존되고 반복이 재개된다 | **PASS** | 15초 프로세스 공백 후 재오픈 즉시 1회 + 유휴 5회, 7→14행 (`run2_restart.log`) |
| A3 | RMV는 프로세스가 없을 때 돌지 않고 놓친 주기를 메우지 않는다 | **확인(제약)** | 공백 15초 동안 refresh 0건 |
| A4 | remote() RMV는 CREATE 시 원격 구조를 조회한다 | **확인(제약)** | source 불가 시 `519 NO_REMOTE_SHARD_AVAILABLE` → 등록 후 생성, 실패 시 retrying 재시도 |
| A5 | chDB는 프로세스당 path 1개, foreground 쿼리 직렬, RMV 백그라운드는 비차단 | **확인** | 2초 probe 중 다른 세션 쿼리 1.8초 대기, RMV 4초 refresh 중 foreground 최대 3ms (`phase_a2/sessions_params.log`) → probe를 subprocess로 격리 |
| A6 | `view_refreshes.last_success_time`은 refresh 시작 시각이다 | **PASS** | 3초 refresh: 시작 .010 → last_success 같은 초, last_refresh는 종료 (`view_refreshes_time_semantics.log`) |
| R1 | NC(`local_encrypted`) + `remoteSecure(nc, …)` RMV가 실제 CH 26.8에서 TLS(strict, 자체 CA)로 수집한다 | **PASS** | metrics 11회×553행, query_log 태그 8건 (`day0/rmv_inspect.log`) |
| R2 | WHERE·log_comment가 원격으로 push down되고 now()는 chDB에서 평가된다 | **PASS** | 원격 query_log에 `_CAST(epoch,'DateTime')` 상수·`log_comment` 조건, `is_initial_query=0` (`day0/remote_side_collector_query.log`) |
| R3 | source 노드 식별이 수집 앱 hostname으로 대체되지 않는다 | **PASS** | `displayName()`이 원격에서 평가(a1/a2/b1). 같은 머신의 `hostName()`은 세 노드·수집 앱이 모두 동일해 식별자로 쓰지 않음 |
| R4 | NC를 지워도 chDB·MV는 로드되고, NC 재생성 시 RMV가 자동 재개된다 | **PASS** | refresh만 669로 실패, 재생성 후 6083→7742행 (`day0/nc_missing.log`) |
| R5 | `APPEND INCREMENTAL`을 remote source에 쓸 수 있다 | **FAIL (26.9.2.1)** | `Code 36: Incremental refresh source must be a table, not a subquery or table function` (`phase_a2/append_incremental_remote.log`) |
| C1 | 노드별 태그 쿼리의 source·destination 안정화 이벤트 집합이 일치한다 | **PASS** | 44/44, 누락·초과 0. 리뷰 후 다중집합·FINAL 행 수·initial 수(21) 독립 검증까지 PASS (`phase_c/c1_reconciliation.json`) |
| C2 | overlap 재조회의 raw 중복은 Dashboard 집계에서 제거된다 | **PASS** | prefix raw 64 → FINAL 32 (2배), 전체 raw 약 1.3~3배. 집계는 FINAL 뷰만 사용 |
| C3 | 같은 query_id를 재사용한 서로 다른 쿼리가 합쳐지지 않는다 | **PASS** | reuse 2쿼리 × (Start, Finish) 4행 유지 |
| C4 | 분산 쿼리의 논리 요청과 노드별 실행이 구분된다 | **PASS** | clusterAllReplicas: a1 initial 1건 + a2 `is_initial_query=0` 1건 |
| C5 | backend 중단 중 발생한 이벤트가 재시작 후 복구된다 | **PASS (대안 경로)** | 45초 중단 → gap 31초 기록·backfill, 24/24 모두 `collect_path=backfill` (`c2_backend_restart.json`) |
| C6 | 노드 1개 중단 시 다른 노드·클러스터 수집과 UI가 계속되고 복구 후 동작한다 | **PASS** | a2만 `refused` 오류, a1·B 계속, Dashboard 29ms 응답, 중단 중 12/12·복구 후 18/18, a2 gap 41초 backfill (`c3_node_failure.json`) |
| C7 | 브라우저 없이 수집이 지속된다 | **PASS** | 3.14시간(`c6_browserless_continuity.log`)에 이어 밤사이 약 7.4시간 연속 수집, 의도한 gap 외 없음 (`c6_overnight_continuity.log`) |
| C8 | 화면 갱신·여러 탭·두 번째 backend가 수집기를 중복 생성하지 않는다 | **PASS** | 2번째 인스턴스 `AlreadyRunning` exit 3, 40회 페이지/API 호출 후 RMV 목록 불변 (`c4_no_duplicates.json`) |
| C9 | 재시작 후 등록·이력 유지, 자격증명 재입력 없이 재연결 | **PASS** | C5 재시작 후 `needs_credentials` 0건, 수집 재개 |
| C10 | 키 누락·불일치 시 복구 모드로 뜨고, 키를 복원하면 재개된다 | **PASS** | missing/다른 키 → recovery(Dashboard 503, 등록 조회 가능), 복원 → normal·수집 (`recovery_mode.json`) |
| S1 | password가 chDB 경로·로그·evidence·API 응답에 평문으로 남지 않는다 | **PASS** | `c5_secret_scan.json` hits 0, NC 파일 암호화. 단 기본(비암호화) NC는 평문 저장됨(실측) |
| D1~D9 | 브라우저 E2E (6절) | **PASS** | `evidence/e2e/*.png`, network/console 로그 |
| N1 | 원격 스트림 도중 끊김 시 INSERT…SELECT 부분 커밋 여부 | **NOT_RUN** | 안정적인 중간 절단 재현 수단을 만들지 못함. 설계는 cursor를 성공한 문장에만 전진시키므로 부분 행은 재조회 시 dedup됨(추론) |
| N2 | 자정을 넘는 overlap의 dedup | **NOT_RUN** | partition을 원본 event_time 기준으로 둬서 같은 이벤트는 같은 partition에 들어감(설계 근거만) |
| N3 | safety lag(8~10초)+overlap보다 늦게 flush되는 query_log의 누락 | **NOT_RUN (측정 안 함)** | 부하 테스트를 하지 않았다. 이 범위를 넘는 지연 도착은 감지되지 않고 누락된다(알려진 한계) |
| N4 | 잘못된 CA·만료 인증서 TLS 오류 | **NOT_RUN** | 분류 규칙(`tls_verify`)만 있음 |
| N5 | Linux·Docker 환경 | **NOT_RUN** | Docker daemon 미기동 |

## 4. 실제 수집 범위와 정확성

- topology: cluster_a(a1, a2) + cluster_b(b1). 축소 없이 다중 노드 수집을 검증했다. 노드 발견은 endpoint의 `system.clusters` 중 `is_local=1`인 클러스터 정의를 쓰고, 같은 계정으로 각 노드를 직접 검사했다. 수집 범위 표시는 "확인 노드/발견 노드"이다.
- query_log 수집 구간: RMV가 매 refresh마다 `[now()-lag-W, now()-lag)`를 읽는다(W = refresh + overlap). 앱은 refresh 성공 시각 S로 cursor `committed_hi = S - lag`를 전진시킨다. 연속 성공 간격이 W를 넘으면 `[committed_hi, 새 구간 하한)`을 gap으로 기록하고 backfill한다(청크 5분, 실패 시 backoff, 24시간 초과분은 unrecoverable). **cursor는 max(event_time)이 아니라 "성공한 문장이 읽은 구간의 끝"**이며, 이벤트가 없는 구간에서도 전진한다.
- 중복: raw는 의도적으로 중복된다(2~3배). identity `(cluster, node, event_time_microseconds, type, query_id, query_start_time_microseconds)` + ReplacingMergeTree + FINAL로 집계 전에 제거한다.
- 집계 정의(Dashboard에도 표시): 기록된 initial 쿼리 수 = `is_initial_query=1 AND type!='QueryStart'`, 실패 = Exception* initial, 노드 실행 이벤트 = 모든 노드의 비-QueryStart, p95 = initial QueryFinish의 quantileExact(0.95)와 표본 수.
- 수집기 자신의 쿼리는 `log_comment='mini-loghouse-collector'`로 원격 query_log에 남고, 수집 단계에서 제외된다.
- 데이터 없음과 실제 0의 구분: 수집 구간(cursor·gap)이 없는 기간은 `미수집`으로 음영 표시한다. 수집 완료 구간의 0은 `실제 0`으로 표시한다.

## 5. 자원 사용 (테스트 설정: refresh 5초, 3노드, 약 3시간)

| 항목 | 측정 |
|---|---|
| backend RSS | 약 286 MB (chDB 내장 엔진 포함), 유휴 CPU 0.1% |
| chDB 데이터 | 25 MB (metrics 노드당 553행/refresh, TTL metrics 3일·query_log 7일) |
| ClickHouse 노드 RSS | 17~100 MB (macOS 압축 메모리 기준 ps 값) |
| 설치 디스크 | venv 583 MB, ClickHouse 바이너리 851 MB |
| 연결 검사 | 정상 약 0.3초, timeout 경로 최대 약 10초 (원격 설정 connect 1~2초, receive 5초) |

## 6. 브라우저 E2E (Playwright, Chromium)

| 시나리오 | 결과 | 증거 |
|---|---|---|
| 빈 목록 → 클러스터 추가 안내 | PASS | e2e-01 |
| 잘못된 password → 인증 실패(516) 표시·저장 비활성 → 수정 후 성공 | PASS | e2e-02, e2e-03 |
| 저장 → `준비 중`·`첫 수집 대기`(KPI `미수집`) → `수집 중`·실제 차트/표 | PASS | e2e-04, e2e-05 |
| 권한 부족 계정 → 접속 성공과 권한 오류(497) 분리, 저장 불가 | PASS | e2e-06 |
| endpoint 실패: 닫힌 포트 → 즉시 `refused` / HTTP 포트 → `http_endpoint` 또는 timeout | PASS (판별 한계 있음, 7절) | 스냅샷 로그 |
| 2노드 클러스터 등록(a2 자동 발견) + 저장 더블클릭 → 등록 1건 | PASS | e2e-07, backend log POST 1회 |
| 기간·노드 필터 → API 파라미터 변경, KPI 31→13, 표 a2만 | PASS | e2e-08, e2e-network-filters.txt |
| 노드 a2 중단 → a2 `수집 오류` + 연속 실패 수, 클러스터 B 정상, 과거 차트 유지·stale 표시 | PASS | e2e-09, e2e-10 |
| 중복 등록 → 409 안내, password 입력란 비움, DOM·URL·storage·cookie에 password 없음 | PASS | e2e-11 |
| 기간 coverage: 24시간 범위의 수집 전 구간 → `부분 집계 (노드별 32%)`, 수집 완료 구간의 0건 → `실제 0` | PASS | e2e-12, e2e-13 |
| 콘솔 | JS 예외 0. 리소스 오류는 의도한 테스트(백엔드 중단 중 자동 갱신, 삭제된 클러스터 404, 409)뿐 | e2e-console-all.log |

E2E 중 발견해 고친 결함: `api()`가 body가 있어도 GET을 기본으로 써서 브라우저가 요청을 보내지 않음(httpx API 테스트로는 잡히지 않음), 정적 파일 캐시로 수정이 반영되지 않음(no-cache + 버전 쿼리), 빈 목록에서 정책 문구 미갱신, 축 눈금 반올림, 복구 모드에서 마지막 상태가 `수집 중`으로 보이던 문제.

## 7. 리스크와 남은 한계

1. **다운타임 복구는 RMV가 아니라 앱 backfill이 담당**한다. RMV만으로는 재시작 후 `W`보다 긴 공백을 메우지 못한다. backfill은 원격 query_log 보존 기간 안에서만 가능하다.
2. **지연 도착**: flush가 `lag + overlap`보다 늦은 이벤트는 감지되지 않고 누락된다(N3 미측정). 부하 환경에서 overlap 근거를 측정하는 감사 재조회가 필요하다.
3. **시계 기준**: 구간 경계는 chDB 시계로 계산되어 원격에 상수로 전달된다. 원격과의 clock skew가 구간 오차가 된다(이번엔 같은 머신).
4. **chDB 직렬 실행**: foreground 쿼리가 직렬이라 backfill 청크·Dashboard 쿼리가 서로 대기한다(청크 1개/틱으로 제한). 노드·이력이 크게 늘면 지연이 커질 수 있다.
5. **TLS 신뢰 설정이 chDB 전역**이라 클러스터마다 다른 사설 CA를 줄 수 없다.
6. **HTTP 포트 오입력 판별이 비결정적**이다. Hello 패킷 내용에 따라 102(got 72) 또는 timeout으로 나타나므로, timeout은 원인 미확정으로 안내한다.
7. **비밀 보호 범위**: NC 암호화는 "키 없이 data dir만 유출"을 막는다. 같은 OS 사용자, 수집된 SQL 원문, 브라우저 자동화 도구 산출물(Playwright 스냅샷이 입력값을 평문 기록함, 이번에 삭제)은 범위 밖이다. NC DDL syntax error는 이스케이프된 password가 포함된 SQL을 되돌려주므로 원본 예외를 노출하지 않도록 했다.
8. **macOS 27 chDB 패치**: 공식 wheel이 수정될 때까지 setup 단계의 바이너리 패치가 필요하다. 패치가 공식 지원 경로는 아니다.
9. **범위 밖**: 다중 사용자 인증·인가, 원격 공개 배포, 수정·일시정지 UI(삭제만 있음), 알림, CPU·디스크 지표(수집하지 않으므로 표시하지 않음).

## 8. 대안 비교 (확인된 제약 A3·A4·R5 기준)

| 대안 | 해소하는 제약 | 비용·위험 | 이번 PoC에서의 사용 |
|---|---|---|---|
| 앱 timer + chDB `INSERT … SELECT remote()` | 상태 기반 구간(cursor) 수집, 다운타임 backfill, 부분 실패 재시도 | 스케줄러·재시도·취소를 앱이 구현. chDB 직렬 실행 때문에 UI와 경합 | **gap backfill에만 사용**. 정상 수집까지 옮기면 RMV 원안이 필요 없어진다(council 권고안) |
| 중앙 ClickHouse server + RMV + remote() | 수집기 수명을 서버 데몬으로 분리, foreground 직렬 제약 해소 | 별도 서버 운영(앱 내 chDB 원안과 다른 아키텍처). 다운타임 backfill 문제는 동일 | 미구현. 원안이 동작했으므로 실험하지 않음 |

권고: 정상 상태 수집은 RMV 원안으로 충분히 동작했다. 실서비스로 확장한다면 다음 두 가지를 먼저 결정해야 한다. ① 지연 도착 감사 재조회와 overlap 근거 측정 ② gap backfill 경로를 앱 루프로 유지할지, 정상 수집까지 앱 루프로 통일할지(구현 단순성과 책임 일원화 대 원안 유지).

## 9. 설계 검증 과정

- council(Claude×Codex 2라운드, 합의도 80)은 query_log를 Python 루프로 옮기라고 권고했다(`docs/council-result.md`). 명세 8절이 "검증 전 대체 금지"를 요구하므로 RMV 고정 구간 + 앱 cursor/gap + 명시적 backfill을 채택했다. 그 밖의 합의(probe 격리, Host/Origin 방어, 키 자동 재생성 금지, 라벨 정정 등)는 반영했다.
- Codex 코드 리뷰: 10절 참조.

## 10. Codex 코드 리뷰 반영

배럭 규칙(설계 council → Claude 구현 → **Codex 리뷰** → Claude 반영·검증)에 따라 `codex exec -s read-only`로 리뷰를 받았다(`docs/codex-review.md`). 12건을 모두 코드·테스트로 확인했고 **전부 수용**했다.

| # | 지적 | 확인 | 조치 | 회귀 방지 |
|---|---|---|---|---|
| P1-1 | 재시작 시 설정이 바뀌면 기존 RMV 구간과 cursor 계산이 어긋나 읽지 않은 구간이 완료로 표시됨 | 반례 재현 | 노드별 RMV 사양(SQL 버전·refresh·lag·window)을 SQLite에 저장. 기동 시 다르면 RMV 재생성. cursor는 저장 사양 사용 | `test_cursor_uses_stored_spec_not_current_settings`, 실환경 마이그레이션 로그(`backend-review.log`) |
| P1-2 | 장애 복구 backfill이 `committed_hi`부터 시작해, overlap 안에서 늦게 flush된 꼬리 이벤트를 놓침 | 반례 재현 | gap 시작을 overlap만큼 되감음 | `test_late_arrival_just_before_cursor_is_inside_recovery_gap` |
| P1-3 | `event_date`(source 시간대)를 chDB 시간대 날짜와 비교해 시간대가 다르면 행이 빠짐 | 코드 확인(이번 환경은 같은 시간대라 미발현) | 날짜 보조 조건을 ±1일로 넓히고, 정확한 필터는 `event_time`(절대 시각)으로만 | RMV 버전 2 |
| P2-4 | 테스트 서버 `preprocessed_configs`에 password 평문 복제 | 실측 12건 | `hide_in_preprocessed="true"`, C5 스캔에 `var/testenv` 추가 | 재기동 후 0건 |
| P2-5 | 삭제와 supervisor 재시도가 경합해 고아 RMV 가능 | 코드 확인 | 공통 lifecycle lock + `deleting` 상태 선기록, 기동 시 중단된 삭제 마무리 | — |
| P2-6 | data dir lock보다 먼저 key fingerprint 기록 | 코드 확인 | lock을 registry·키보다 먼저 획득 | C4 재실행 |
| P2-7 | 선택 기간 전체가 미수집이어도 `실제 0` 표시 | 코드 확인 | 기간과 수집 구간의 교집합을 서버에서 계산해 none/partial/full로 구분 | `test_range_coverage_none_partial_full` |
| P2-8 | 후보 노드 timeout 시 endpoint 성공 결과까지 소실 | 코드 확인 | probe가 대상별로 결과를 즉시 내보내고, 부모가 부분 결과를 수습 | `test_probe_timeout_keeps_endpoint...` |
| P2-9 | 권한·테이블 오류를 `연결 실패`로 표시 | 코드 확인 | 연결 상태를 정상·실패·미확인 셋으로 분리 | — |
| P2-10 | 오류 분류기가 사용자명·SQL 문자열을 근거로 씀 | 반례 재현(`DNS_ERROR` 사용자) | 가장 안쪽 ClickHouse 코드 우선, 메시지 패턴은 네트워크 코드 구간에만 적용 | `test_user_strings_cannot_select_category` 외 2 |
| P2-11 | 오래된 PID 파일로 무관한 프로세스 종료 가능 | 코드 확인 | PID+시작 시각 대조(ClickHouse watchdog이 프로세스명을 바꿔 명령줄 검증은 불가했음) | — |
| P2-12 | Phase C가 set 비교라 중복 행을 놓치고 실패해도 exit 0 | mock 반례 | 다중집합 비교, FINAL 행 수·initial 수 독립 검증, 실패 시 exit 1 | 재실행 결과(11절) |

Codex가 "중대 결함 없음"으로 본 영역: Dashboard dedup 집계, SQL injection/identifier, static JS XSS, 일반 API 응답의 password 노출.

## 11. 리뷰 반영 후 재검증 (2026-10-28)

| 검증 | 결과 | 근거 |
|---|---|---|
| 단위 테스트 | 55 PASS (리뷰 반례 회귀 테스트 포함) | `pytest` |
| RMV 사양 마이그레이션 | 기존 3노드 RMV가 `spec None → v2`로 재생성되고 수집 지속 | `evidence/phase_c/backend-review.log` |
| Phase C 전체 (강화된 판정: 다중집합·FINAL 행 수·initial 수 독립 계산, 실패 시 exit 1) | **RESULT: PASS, exit 0** — C1 44/44, C2 24/24(gap이 되감기 반영으로 46초), C3 장애 중 12/12·복구 후 18/18, C4 lock 거부·RMV 불변, C5 테스트 서버 경로까지 평문 0 | `evidence/phase_c/run-after-review.log`, `c1~c5*.json` |
| 복구 모드 재검증 | 키 누락·불일치 시 상태가 `error`(복구 모드)로 표시되고 Dashboard 503, 키 복원 시 재개 | `recovery_mode.json` |
| 무브라우저 장기 수집 | 2026-10-27 22:16 → 10-28 05:41, **약 7.4시간 연속**. gap은 의도한 노드 중단·재기동 2건뿐이고 모두 backfill됨 | `c6_overnight_continuity.log` |
| 기간 coverage UI | 24시간 범위에서 `부분 집계 (노드별 32/32%)` 표시, 수집 시작 전 구간은 `수집 전` 음영 | `evidence/e2e/e2e-12-a-24h-partial-coverage.png` |
