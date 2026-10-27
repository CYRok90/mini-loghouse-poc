# LLM Council Report

| Field | Value |
|-------|-------|
| Session | `20261027_183726_70098` |
| Mode | debate |
| Rounds | 2 |
| Agents | Claude Codex |
| Consensus | 85 |
| Started | 2026-10-27T09:37:26Z |
| Completed | 2026-10-27T09:44:38Z |

## Consensus Trajectory

- Round 2: **80**/100 — 핵심 아키텍처(metrics RMV + query_log Python 수집, 저장 후 cursor 커밋, sentinel 철회, partition·dedup·gap 기록, 키 비파괴 복구)에 실질 합의했으나, LIMIT 청크 처리 방식(A의 동률 규칙 vs C의 LIMIT 없는 시간구간), CSRF 토큰 유지 여부, 원격 now() 기반 hi의 위상, 하이브리드 포기 기준 등 세부 쟁점이 남음

## Topic

# Council brief — chDB Mini LogHouse 웹 PoC 구현 설계 검토

각 참여자는 **자신의 의견만** 작성하라. 다른 참여자 발언이나 가상 토론 라운드를 재연하지 마라.
목표: 아래 설계 초안의 결함(정확성·장애복구·보안·과잉설계)을 찾아 구체적 수정안을 제시. 각 지적은 "문제 → 실패 시나리오 → 수정안" 형식.

## 요구 (요약)
단일 운영자 로컬(loopback) 웹앱. 사용자가 웹에서 ClickHouse 클러스터 endpoint/TLS/user/password 등록 → 연결·권한 검사 → 저장·수집 시작 → 목록에서 상태(준비 중/첫 수집 대기/수집 중/수집 지연/수집 오류, 마지막 성공 시각, 노드 커버리지 전체/일부/미확인) → 상세 Dashboard(현재 metric+시계열, 기간 쿼리 수/실패 수, p95, 시간대별 추이, 최근/느린/오류 쿼리 테이블; 기간 15m/1h/24h, 노드 필터). 수집 원안은 **앱 내 chDB + Refreshable MV(RMV) + remote()/remoteSecure()**. source는 system.metrics(snapshot APPEND), system.query_log(구간·중복·지연·재시작 복구 관리)만. Dashboard는 chDB 저장 데이터만 조회. 브라우저가 닫혀도 backend가 살아 있으면 수집 지속. password는 URL/localStorage/API 응답/로그 노출 금지, 재시작 후 보존 시 암호화+키 분리. 테스트 topology: 로컬 ClickHouse A(2노드) + B(1노드).

## 실측된 사실 (이 머신, 2026-10-27)
- macOS 27 arm64, chdb 4.4.0 / chdb-core 26.9.0 / engine 26.9.2.1 (Mach-O strtab 정렬 패치 후 import 가능). 테스트 서버는 ClickHouse 26.8 LTS 예정.
- 영속 Session에서 `REFRESH EVERY 5 SECOND APPEND TO` RMV: SQL 없는 유휴 22초 동안 5초 경계 refresh 4회. 프로세스 15초 완전 종료 동안 0회, 재오픈 시 즉시 1회 + 이후 주기 반복. 데이터/정의 보존. ⇒ RMV는 상주 프로세스 수명 동안만 돌고, 다운타임 backfill 없음.
- remote() source RMV는 CREATE 시 원격 구조를 조회 → source 불가 시 생성 실패(519).
- 오류코드: 거부 210, connect timeout 209, DNS 198 (외곽 519/279로 감싸짐). 기본 3회 재시도. 메시지에 password 미포함.
- 기본 named collection(NC)은 `<path>/named_collections/*.sql`에 password 평문. `?config-file=` 로 `named_collections_storage type=local_encrypted, key_hex from_env` 지정 시 암호화 저장, 재시작 후 로드됨. 키 없음/틀림이면 chDB 전체 open 실패.
- query parameter `{name:Type}` 바인딩 지원(chdb Session.query params).

## 설계 초안
1. **스택**: Python 3.11 + FastAPI/uvicorn(단일 worker, reload 금지) + 정적 HTML/vanilla JS(빌드 없음, 차트는 자체 SVG, 외부 CDN 없음). 등록 metadata는 SQLite(stdlib), telemetry는 chDB. 127.0.0.1 bind.
2. **수명/소유권**: 프로세스당 chDB Session 1개를 `CollectorOwner`가 소유. data dir에 fcntl 배타 lock(두 번째 인스턴스는 즉시 종료). HTTP 요청은 RMV를 생성/재생성하지 않음(등록 시 1회 DDL, 시작 시 reconcile만). 원격 probe(연결 테스트)는 별도 Session 또는 thread에서 timeout·동시성 제한.
3. **자격증명**: password는 SQLite에 저장하지 않고 chDB NC(local_encrypted)에만 저장. 키는 data dir 밖 `~/.config/mini-loghouse/nc.key`(0600, 최초 생성) 또는 env. RMV DDL은 `remoteSecure(nc_<cid>_<nid>, database='system', table='query_log')`처럼 NC 이름만 참조 → DDL/metadata에 password 없음(서버 확보 후 실측 예정). 연결 테스트 probe는 parameter binding으로 password 전달. 키 분실 시: named_collections 디렉터리 격리 후 재입력 상태로 복구.
4. **topology**: 등록 endpoint에서 `system.clusters`로 노드 후보 발견 → 각 노드에 같은 계정으로 probe → 성공 노드마다 NC + RMV 생성. 커버리지 = 성공 노드 / 발견 노드 (발견 불가 시 '미확인', endpoint만). 노드별 RMV라 한 노드 장애가 다른 노드 수집을 막지 않음.
5. **metrics**: 노드별 RMV `REFRESH EVERY 15 SECOND APPEND TO telemetry.metrics_raw AS SELECT '<cid>','<nid>', hostName() AS source_host, now64(3) AS collected_at, metric, value FROM remoteSecure(nc,...system.metrics)`.
6. **query_log 증분**: 노드별 RMV, 대상 `telemetry.query_log_raw`(ReplacingMergeTree). window: `lo = greatest(watermark - overlap, now() - max_backfill)`, `hi = now() - safety_lag`. watermark는 RMV 내부 scalar subquery로 로컬 테이블에서 `max(event_time)`(해당 node) 읽음(초기화 시 now()-max_backfill). safety_lag 15s(> flush_interval 7.5s), overlap 60s, max_backfill 24h. 원격 스트림 `ORDER BY event_time_microseconds`로 부분 실패 시 prefix만 삽입되도록 함. 행에 window_lo/hi, collected_at 기록. ⇒ 재시작 후 첫 refresh가 다운타임 구간을 자동 backfill(최대 24h).
   - dedup key: (cluster_id, node_id, event_time_microseconds, query_id, type). QueryStart/Finish는 type으로 구분. 집계는 FINAL 또는 key GROUP BY.
   - 수집기 자체 쿼리는 `log_comment='mini-loghouse-collector'`로 표시하고 Dashboard 집계에서 제외.
7. **집계 정의**: 논리 요청 = is_initial_query=1 AND type∈{QueryFinish, ExceptionBeforeStart, ExceptionWhileProcessing}. 노드별 실행 = 모든 노드의 같은 type 집합(is_initial_query 무관). 실패 = Exception* (initial). p95 = QueryFinish initial의 query_duration_ms. QueryStart는 집계 제외.
8. **상태**: 앱 supervisor가 `system.view_refreshes`를 주기 폴링(로컬 chDB 조회만) → 노드/소스별 last_success, last_exception, next_refresh를 chDB `telemetry.collection_status`에 append(재시작 후 이력 보존). 상태: RMV 생성 전=준비 중, 생성 후 성공 없음=첫 수집 대기, last_success ≤ 3×interval+lag=수집 중, 초과=수집 지연, 최근 refresh exception=수집 오류. 연결/수집/관측 클러스터 상태 분리 표시.
9. **보안**: 모든 동적 텍스트는 textContent 렌더. POST에 Origin 검사 + double-submit CSRF 토큰. 등록 idempotency key + (host,port,user) unique → 중복 등록 방지. identifier(DB/NC/MV 이름)는 서버 생성 hex만. 사용자 입력(host/port/user)은 정규식·범위 검증 후 리터럴은 parameter binding 또는 검증된 quoting. data dir 0700.

## 검토 요청 질문
Q1. 6번 watermark(scalar subquery + ORDER BY prefix) 설계가 원격 table function 안에서 실제로 의도대로 동작할 가능성과 실패 시나리오, 더 단순하고 정확한 대안은?
Q2. DDL이 정적이라 사용자 값(host·NC 이름·cid/nid 리터럴)을 RMV DDL에 넣어야 한다. 안전한 생성 방식은? parameter binding이 DDL에 불가능할 때의 quoting 정책?
Q3. 2·3번의 소유권/동시성/키 관리에서 빠진 실패 모드?
Q4. 7번 집계 정의의 오류(분산 쿼리, 재시도, 동일 query_id 재사용 등)?
Q5. PoC 범위 대비 과잉설계로 빼야 할 것, 반드시 추가해야 할 것?

---

## Synthesis

# chDB Mini LogHouse 설계 검토: 최종 합의안

두 전문가(A: Claude, C: Codex)의 2라운드 토론을 종합했습니다. 라운드 2에서 양측이 상당 부분 수렴했고, 남은 이견은 구현 세부와 복구 정책에 한정됩니다.

---

## 1. 합의된 사항

### 1.1 수집 아키텍처
- **metrics는 RMV를 유지합니다.** 노드별 `REFRESH EVERY 15 SECOND APPEND`로 수집합니다. 상주 프로세스 동안 주기 실행된다는 효용이 실측으로 확인되었습니다.
- **query_log는 Python 수집 루프로 옮깁니다.** 원안의 RMV + scalar watermark + `ORDER BY` prefix 방식은 채택하지 않습니다. 이유는 다음과 같습니다.
  - `max(event_time)`은 "관측한 최대 시각"일 뿐, "그 이전 구간을 빠짐없이 수집했다"는 증거가 아닙니다.
  - 유휴 노드에서는 watermark가 멈추고, 그 결과 window가 계속 넓어집니다.
  - ORDER BY 정렬과 실패 원자성은 서로 다른 성질입니다. 부분 삽입이 prefix가 된다는 보장이 없습니다.
  - scalar subquery가 원격에 전송되는지, 필터가 원격으로 push down되는지 모두 미확인입니다.
- **A가 제안한 RMV sentinel(UNION ALL) 방식은 A 스스로 철회했습니다.** 분기 간 삽입 순서와 원자성이 보장되지 않기 때문입니다.
- **query_log RMV가 필수 요구라면** 허용되는 형태는 하나뿐입니다. watermark 없이 매번 고정된 최근 구간을 재조회하고 dedup하며, 긴 다운타임은 별도의 명시적 backfill로 처리합니다.

### 1.2 완료 cursor와 불변식
- 진행 상태는 데이터와 분리하여 **SQLite의 `committed_hi`**에 둡니다.
- 커밋 순서는 **원격 조회 완료 → 로컬 저장 성공 → cursor 커밋**입니다. 중간에 실패하면 같은 구간을 재조회하고, 생기는 중복은 dedup으로 수렴합니다.
- 구간은 **`[lo, hi)` 반열림**으로 표기하고, 비교 타입은 `DateTime64(6)` 하나로 통일합니다.
- 빈 구간도 성공으로 처리하여 cursor를 전진시킵니다. **"쿼리 0건"과 "수집 못 함"을 구분**하는 것이 이 설계의 핵심입니다.
- `max_backfill`(24h)을 넘어 복구하지 못한 구간은 `gaps(node, from, to, reason)`에 기록하고, 차트에 "미수집"으로 음영 표시합니다.
- `safety_lag`와 overlap은 완전성 조건이 아니라 지연 완화 장치입니다. 완전성 주장은 "원격 보존 기간 안에서, 재조회 범위에 들어온 이벤트"로 한정합니다.
- **승인 기준 문장**: "수집이 불완전하면 그 구간을 다시 읽거나, 누락 구간을 사용자에게 명시한다. 그 판단은 데이터가 아니라 별도 cursor와 gap 기록에서 나온다."

### 1.3 저장 스키마와 집계
- ReplacingMergeTree의 **`PARTITION BY`는 원본 이벤트 시각 기준**으로 합니다. `collected_at` 기준이면 자정을 넘는 overlap 중복이 영구히 남습니다. TTL도 같은 기준을 씁니다.
- `ORDER BY`(dedup identity)에서 `collected_at`과 window 정보는 제외합니다. 대신 `query_start_time_microseconds`를 추가합니다. 이렇게 해도 절대 유일성은 아니므로 한계를 문서화합니다.
- 노드 정체성은 주소나 alias가 아니라 서버가 생성한 nid로 둡니다. 중복 노드 점검에는 probe로 관측한 서버 식별 정보(`serverUUID()` 등)를 씁니다.
- **모든 Dashboard 쿼리는 dedup 뷰 하나를 경유합니다.** dedup(`FINAL` 등)은 집계보다 먼저 적용합니다.
- 집계는 행 단위 count로 합니다. `uniqExact(query_id)`로 세면 query_id를 재사용하는 경우가 틀어집니다.
- 명칭과 라벨을 다음과 같이 정정합니다.
  - "논리 요청" → **"기록된 initial 쿼리 수"** (재시도는 별개로 세어짐)
  - 노드 합계 → **"노드 실행 이벤트 수"**
  - p95 → **"성공 쿼리 지연 p95"** (표본 수 함께 표시)
  - 전체 p95는 원본 duration을 합쳐서 계산합니다. 노드별 p95를 평균하지 않습니다.
- 수집기 자신의 쿼리는 원격에서 `is_initial_query=0`으로 기록되므로 "노드 실행" 지표를 오염시킵니다. 설치별 랜덤 `log_comment`를 붙이고 수집 단계에서 제외합니다. 이 값이 원격까지 전파되는지는 실측해야 합니다.
- 저장과 집계는 UTC로 하고, 표시 타임존은 분리합니다.
- 관측 범위를 함께 표시합니다. 노드 커버리지, query_log 설정 제약(sampling, 최소 실행 시간), 인증 실패가 query_log에 없다는 점이 여기에 해당합니다.

### 1.4 DDL과 자격증명
- RMV DDL에는 **서버가 생성한 hex identifier와 내부 ID만** 넣습니다. host, user, password는 NC에만 들어갑니다.
- identifier 정책과 문자열 리터럴 정책을 분리합니다. 문자열 serializer는 하나로 중앙화하고 `\`와 `'`를 모두 이스케이프합니다. NUL과 제어문자는 거부합니다.
- parameter binding은 injection 방지 수단이지 비밀 보호 수단이 아닙니다. 요청 body, SQL, params, 원본 예외는 로그와 응답에 남기지 않습니다. 오류는 분류된 코드와 고정 메시지로 치환합니다.
- password round-trip 테스트를 둡니다. 특수문자, 유니코드, 긴 값으로 NC를 만든 뒤 실제 로그인에 성공하는지 확인합니다.

### 1.5 소유권, 동시성, 복구
- probe는 **별도 subprocess**로 실행합니다. chDB의 다중 Session 제약 문제를 피하고, hang과 크래시를 격리하며, 하드 kill이 가능하기 때문입니다. password는 stdin으로 넘기고, 키 환경변수는 상속하지 않습니다.
- Session 호출은 **전용 executor 또는 lock으로 직렬화**합니다. Dashboard 쿼리에는 실행 시간과 행 수 제한을 둡니다.
- lock은 **chDB open 전에** 잡고, FD를 프로세스 수명 동안 유지합니다. `fcntl.flock`을 쓰고, lock 파일은 다른 곳에서 열거나 교체하지 않습니다.
- 등록 과정은 작은 상태 머신(`pending → provisioning → active / needs_attention`, deleting)으로 만듭니다.
  - 기동 시 reconcile에서 chDB의 NC/RMV와 SQLite를 대조합니다.
  - 등록 시점에 원격이 응답하지 않아 519가 나면 backoff로 재시도합니다.
  - 삭제 순서는 수집 중단 → NC 제거 → SQLite 정리입니다.
- **키를 자동 재생성하지 않습니다.** 키는 배타 생성, 0600, 원자적 저장으로 만들고 부모 디렉터리는 0700으로 둡니다. 보호 목표는 "키 없이 data dir만 유출된 경우"로 명시합니다.
- 원격 hang에 상한을 둡니다. `max_execution_time`, `receive_timeout`, `connect_timeout`을 지정하고, RMV 자체 재시도는 끕니다.
- TTL과 보존 기간을 반드시 둡니다(예: metrics 3일, query_log 7일). 동시 수집 수도 제한합니다.

### 1.6 topology
- `system.clusters`는 **후보 목록**일 뿐입니다. 내부 관점의 host/port(docker 이름, 9000번 포트)나 기본 예제 클러스터가 섞여 있습니다.
- 사용자가 클러스터를 선택하고 노드별 접속 주소를 확정합니다. **커버리지 분모는 이 확정 목록**입니다.
- 자동 발견에 실패하면 "단일 endpoint 수집"으로 명확히 표시합니다.

### 1.7 보안
- **Host 헤더 allowlist**를 추가합니다(DNS rebinding 방어, 위반 시 421). Origin 검사, JSON 강제, CORS 미설정을 함께 둡니다. 상태 변경은 GET으로 하지 않습니다.
- **query_log의 쿼리 원문은 비밀 데이터**로 취급합니다. NC 암호화는 telemetry에 저장된 SQL 원문을 보호하지 않습니다. 표시 길이를 제한하거나 축약하고, 보존 기간을 정합니다.
- TLS 검증 설정은 NC 단위가 아니라 chDB 전역 설정일 가능성이 큽니다(A 지적, C 이견 없음). UI에서 정직하게 표현합니다.

### 1.8 상태와 과잉설계 정리
- 상태 정보를 셋으로 분리합니다: **마지막 시도 결과와 성공 시각 / 검사 완료 구간의 끝(`committed_hi`) / 마지막 관측 이벤트 시각**. "지연" 판정은 `committed_hi` 기준으로 합니다. 그래야 유휴 노드가 지연으로 오판되지 않습니다.
- `collection_status`를 매 폴링마다 append하는 방식은 제거합니다. **SQLite에 최신 상태를 두고, 상태가 전이될 때만 이력을 남깁니다.** chDB가 열리지 않아도 상태를 읽을 수 있어야 하기 때문입니다.
- 클러스터 상태는 노드 상태 중 최악값으로 하고, 커버리지 배지는 별도로 둡니다.
- 등록 idempotency key는 제거합니다. unique 제약과 버튼 disable로 충분합니다. 키는 env와 파일 두 경로 중 **파일 경로 하나만** 지원합니다.
- 반드시 추가할 것: 클러스터 삭제·일시정지, 진단 페이지(노드별 window, 행 수, skew, 오류코드).

### 1.9 Day 0 스파이크
아키텍처 분기점이 되는 미확인 동작은 UI 구현 전에 검증합니다. 검증은 **chDB 4.4.0(engine 26.9.2.1)이 ClickHouse 26.8 서버에 접속하는 실제 조합**으로 해야 합니다. 서버 단독 검증으로는 부족하다는 C의 보강을 수용했습니다.

---

## 2. 미합의 쟁점

| 쟁점 | A 입장 | C 입장 |
|---|---|---|
| **청크 수집 방식** | `ORDER BY … LIMIT` 청크. 경계 규칙을 정정함: LIMIT에 꽉 차면 `>= last_ts` 포함 경계로 이어서 읽고, 청크 전체가 같은 ts이면 해당 ts만 LIMIT 없이 1회 읽은 뒤 +1µs | LIMIT을 쓰지 않음. **시간 구간을 잘게 나누어 각 구간을 끝까지 읽은 뒤에만** cursor 전진. 제한에 걸리면 구간을 더 작게 나누어 재시도 |
| **CSRF 토큰** | 제거. JSON 강제 + CORS 미설정이면 cross-site 요청이 아예 전송되지 않음. rebinding 상황에서는 공격자가 cookie와 header를 모두 설정할 수 있어 double-submit은 무력함 | 라운드 1에서 JSON + CSRF + SameSite 유지를 주장. 라운드 2에서 A의 반론에 재반박하지 않음 |
| **키 오류 복구** | fingerprint가 불일치하면 NC를 quarantine으로 옮기고 새 키를 생성한 뒤 "재입력 필요" 상태로 자동 전환 | 자동 전환 반대. 경로 오류나 일시적 접근 오류를 키 유실로 오인할 수 있음. **엔진을 열지 않는 복구 모드**로 띄우고, 운영자가 "키 복원" 또는 "자격증명 초기화"를 명시적으로 선택하게 함 |
| **metrics snapshot 완전성** | `collected_at`별 행 수가 최빈값 이상인 마지막 snapshot만 사용 | 행 수 추정은 정확성 판정 근거가 될 수 없음. `snapshot_id`와 완료 기록을 쓰거나, "최근 수집값"으로만 표시 |
| **`hi`의 기준 시계** | 원격 `now()` 기준으로 고정. 원격 시계가 늦으면 로컬 기준 `hi`에서 누락이 생김 | 개선은 맞지만 왕복과 장애 지점이 추가되고, 지연 도착 문제는 해결하지 못함. skew 측정은 진단용으로 두고 완전성 주장 범위를 좁힘 |
| **기동 시 하한 클램프** | MergeTree는 기본 설정에서 fsync하지 않으므로, 전원 장애 후 cursor가 데이터보다 앞설 수 있음. 기동 시 `lo = min(committed_hi, local_max) - overlap`으로 클램프 | 영속성 의미를 전제 조건으로 명시해야 한다는 데는 동의. 클램프 자체는 다루지 않음 |
| **endpoint 입력 검증** | host, user에 정규식 적용 | 정규식은 ClickHouse 주소 패턴 문법을 막지 못하고 정상 계정까지 배제할 수 있음. SQL 안전성과 endpoint 의미 검증을 분리하고, host는 단일 DNS 이름 또는 IP로 **파싱**함 |
| **비밀 유출 검증 기준** | data dir과 로그를 grep해서 0건이어야 함 | 암호화된 NC 안에는 존재할 수 있음. 평문 로그, SQL metadata, 응답에 없어야 한다는 것이 정확한 기준이며, grep 0건만으로는 암호화를 검증할 수 없음 |
| **구현 비용** | "스케줄러 약 60줄 교체" | 지나치게 낙관적. 취소, 구간 폭 조절, 재시도, 삭제와의 경합까지 구현해야 함 |
| **오류 판정 임계** | 연속 2회 실패부터 "오류" | 정책으로는 수용. 다만 첫 실패 사실은 즉시 노출해야 함 |

---

## 3. 최종 권고안

미합의 쟁점에 대한 판단부터 정리합니다.

- **청크 방식은 C의 시간 구간 분할을 채택합니다.** A의 정정된 규칙도 동작하지만 동률 처리와 특수 경로가 필요합니다. 시간 구간을 끝까지 읽는 방식은 불변식이 "이 구간을 다 읽었다" 하나로 끝납니다. 구간 폭은 직전 처리량에 맞춰 조절합니다(예: 기본 1분, 실패 시 절반, 성공이 이어지면 두 배, 상한 15분).
- **CSRF 토큰은 제거합니다(A의 안).** cookie 기반 인증이 없는 loopback 단일 운영자 앱에서 double-submit은 방어에 기여하지 않습니다. Host allowlist, Origin 검사, `Content-Type: application/json` 강제, CORS 미설정, 커스텀 헤더 요구로 충분합니다.
- **키 오류 복구는 C의 안을 채택합니다.** 비가역 조작은 운영자가 명시적으로 선택한 뒤에만 수행합니다. fingerprint는 "키가 다름"을 진단하는 용도로만 씁니다. 복구 모드에서도 SQLite의 상태 이력과 gap 기록은 읽을 수 있습니다.
- **metrics는 C의 안을 채택합니다.** "최근 수집값 + metric별 `collected_at`"으로 표시하고 최빈값 휴리스틱은 쓰지 않습니다. 이후 metrics가 Python 경로로 통일되면 완료 기록을 추가합니다.
- **`hi`는 원격 `now()` 기준으로 잡습니다(A의 안).** Python 루프는 어차피 사이클마다 원격과 왕복하므로 추가 비용이 작습니다. 다만 C의 지적대로 이것을 완전성 근거로 내세우지 않습니다. skew가 5초를 넘으면 진단 페이지에 경고합니다.
- **기동 하한 클램프를 채택합니다(A의 안).** 정상 상황에서는 영향이 없고 비용도 거의 없습니다. Day 0에서 강제 종료 시나리오로 실제 동작을 확인합니다.
- **endpoint 검증은 C의 안을 채택합니다.** host는 `ipaddress` 모듈이나 DNS 라벨 파서로 파싱하고, 주소 패턴 문법(`{}`, `|`, `,` 등)은 거부합니다. port는 별도 정수로 받습니다. user와 password는 serializer만 통과시킵니다.

### 실행 순서

**Day 0 스파이크** (chDB 4.4.0 → CH 26.8, 반나절~1일)

| # | 검증 항목 | 실패 시 결정 |
|---|---|---|
| 1 | NC를 참조하는 `remoteSecure`에서 상수 WHERE가 원격으로 push down되는지. 원격 query_log의 `query` 텍스트와 `read_rows`로 확인 | 구간 폭 축소, 컬럼 최소화 |
| 2 | `log_comment`가 원격 query_log까지 전파되는지 | 대체 표식 사용(`initial_query_id` 접두어 등) |
| 3 | NC가 없을 때 RMV metadata가 로드되고 chDB가 열리는지 | **metrics도 Python 루프로 통일** |
| 4 | password round-trip 성공 여부, 평문 로그·SQL metadata·응답에 비밀이 남는지 | serializer 수정, 로컬 `query_log`/`text_log` 비활성화 |
| 5 | 원격 연결을 중간에 끊거나 강제 종료할 때 INSERT…SELECT가 부분 커밋하는지, cursor와 데이터의 관계 | 결과만 기록. 설계가 이 동작에 의존하지 않음을 확인 |
| 6 | 자정을 넘는 overlap에서 dedup 카운트가 정확한지 | partition 키 재검토 |
| 7 | 다중 Session 제약, NC/remote secret masking 적용 여부 | probe subprocess 방침 확정 |

**구현 단위**

1. **기반**: flock(open 전 획득), 단일 Session executor, SQLite 스키마(clusters, nodes(phase), cursors, gaps, state_transitions), 키 로딩과 복구 모드.
2. **등록**: 입력 파싱 → probe subprocess → 후보 노드 확정 UI → NC 생성 → metrics RMV 생성. pending 상태 재시도와 기동 reconcile을 포함합니다.
3. **query_log 루프**: 노드별 asyncio 태스크가 다음 순서로 동작합니다.
   1. 원격 `now()`를 조회합니다.
   2. `[committed_hi − overlap, hi)` 구간에 대해 chDB 안에서 단일 `INSERT INTO raw SELECT <약 15개 컬럼> FROM remoteSecure(nc, system, query_log) WHERE … AND log_comment != {tag}`를 실행합니다. Python이 행을 중계하지 않습니다.
   3. 문장이 성공하면 cursor를 커밋합니다.
   4. 실패하면 구간을 분할하여 재시도합니다.

   timeout을 설정하고, 동시 실행 수를 제한하고, 24h를 넘는 구간은 gap으로 기록합니다.
4. **감사 재조회(A의 1-E, 선택)**: 10분마다 overlap 밖 구간을 재조회하여 늦게 도착한 행 수를 측정하고, 그 값을 진단 페이지에 노출합니다. 이 측정값으로 overlap 설정의 근거를 관측값으로 대체합니다.
5. **Dashboard**: dedup 뷰 하나를 경유하고, 정정된 라벨을 쓰고, UTC로 계산한 뒤 epoch ms로 응답합니다. gap은 음영으로, 커버리지는 배지로 표시합니다. 쿼리 원문은 축약합니다.
6. **보안 미들웨어**: Host/Origin 검증, JSON 강제, 예외 sanitizer를 둡니다. 렌더링은 기존대로 textContent를 씁니다.
7. **수용 테스트**(장애 후 불변식 중심):
   - 부분 수신 후 연결 종료
   - 데이터 저장 직후, cursor 커밋 전 kill
   - overlap보다 늦게 도착한 로그
   - 24h를 넘는 다운타임
   - 키 누락 또는 불일치
   - A 클러스터의 한 노드만 불통
   - 유휴 노드가 "지연"으로 오판되지 않는지

---

## 4. 참고사항 (추가 조사가 필요한 영역)

- **chDB 쓰기 영속성**: 기본 `fsync_after_insert=0`일 때 전원 장애 후 part가 유실되는 범위를 확인해야 합니다. 클램프로 충분한지, fsync 설정을 켜야 하는지 결정이 필요합니다.
- **TLS 설정 범위**: chDB에서 `openSSL.client`가 전역으로만 적용되는지, NC 단위 오버라이드가 가능한지 확인해야 합니다. 클러스터별 사설 CA 지원 여부가 여기에 달려 있습니다.
- **chDB Python 바인딩의 스레드 안전성**: RMV 백그라운드 스레드와 executor 호출이 경합할 때의 동작을 확인해야 합니다.
- **masking**: chDB 빌드에서 NC와 remote secret masking이 적용되는지, 로컬 `system.query_log`/`text_log`에 DDL이 남는지 확인해야 합니다.
- **신규 서버에서 `system.query_log`가 아직 없는 경우**: 첫 flush 전에는 테이블이 없으므로 수집 루프와 probe가 이 상태를 "준비 중"으로 다뤄야 합니다.
- **query_log 지연 도착의 실제 분포**: 부하 상황에서 flush가 얼마나 늦어지는지 측정하여 overlap 기본값의 근거로 삼습니다.
- **metrics 경로 통일 여부**: Day 0 #3 결과에 따라 RMV를 완전히 제거할지 결정합니다. 하이브리드는 스케줄러, 상태 출처, 실패 모드가 각각 두 벌이 된다는 비용이 있습니다.
- **`system.metrics`의 표현 한계**: 시점별 gauge만 제공하므로 CPU 사용률이나 처리량 같은 지표가 필요하면 다른 source를 검토해야 합니다. 이번 범위에서는 제외합니다.

---

## Token Usage

### 라운드별 사용량

| Agent | Round | Input | Output | Cached | Total |
|-------|-------|-------|--------|--------|-------|
| Claude/runtime-default/runtime-default | R1 | 2 | 22724 | 535 | 28844 |
| Codex/runtime-default/runtime-default | R1 | 11295 | 4730 | 0 | 16025 |
| Claude/runtime-default/runtime-default | R2 | 2 | 8311 | 2123 | 36553 |
| Codex/runtime-default/runtime-default | R2 | 23881 | 2652 | 0 | 26533 |

### 한도 현황

| Agent | 한도 정보 |
|-------|-----------|
| Claude | (조회 불가) |
| Gemini | (한도 조회 불가 — Google AI Studio에서 확인) |
| Codex | (한도 조회 불가 — OpenAI 대시보드에서 확인) |

---

## Round Details

### Round 1

### 전문가 A — Claude/runtime-default/runtime-default
# chDB Mini LogHouse 설계 검토

## 핵심 결론

1. **가장 위험한 부분은 6번 query_log 증분입니다.** 진행 상태를 "수집된 데이터의 `max(event_time)`"에서 꺼내는 구조에는 세 가지 결함이 있습니다. 유휴 노드에서는 watermark가 멈추고, 로컬 시계와 원격 시계를 섞어 쓰며, scalar subquery가 원격으로 넘어가는지에 의존합니다. 진행 상태는 데이터와 분리된 명시적 기록으로 바꾸는 것이 맞습니다.
2. **RMV가 이미 해결하는 것은 생각보다 적습니다.** 실측대로 RMV는 상주 프로세스 수명 동안만 돌고, backfill도 하지 않으며, 상태는 결국 Python이 폴링합니다. RMV 사용 근거는 "엔진 안에서 스케줄링된다" 하나뿐입니다. 그래서 다음을 권합니다.
   - metrics(단순 APPEND)는 RMV로 유지합니다.
   - query_log는 RMV 방식을 30분짜리 검증 게이트에 통과시킵니다.
   - 실패하면 같은 SQL을 Python 스케줄러가 parameter binding으로 실행하는 경로로 즉시 전환합니다.

   Python 경로로 가면 Q2(DDL quoting)의 대부분이 사라집니다.
3. **테스트 topology에서 실제로 먼저 깨질 곳은 4번 노드 발견입니다.** `system.clusters`의 host/port는 클러스터 내부 관점 값입니다. docker 등 로컬 환경에서는 노트북에서 도달할 수 없는 경우가 대부분입니다.
4. **보안에서는 CSRF 토큰보다 DNS rebinding(Host 헤더 검증)이 더 중요한데 빠져 있습니다.**

아래 표시: 🔴 정확성/데이터 손실, 🟠 장애복구, 🟡 보안, ⚪ 과잉설계. "실측 필요"는 제가 이 환경에서 검증하지 못한 엔진 동작입니다.

---

## Q1. watermark 설계 (scalar subquery + ORDER BY prefix)

### 🔴 1-1. 유휴 노드에서 watermark 정지 → window 팽창
- **문제**: watermark를 `max(event_time) FROM query_log_raw WHERE node=…`로 정의하면, 해당 노드에 새 쿼리가 없을 때 watermark가 전진하지 않습니다.
- **실패 시나리오**: 테스트 클러스터 B(1노드)가 밤새 유휴 상태입니다. `lo`는 고정된 채 `hi`만 전진해서 매 15초마다 점점 넓은 구간(최대 24h)을 원격에서 재스캔합니다. 더 나쁜 경우도 있습니다. 수집기 쿼리를 소스에서 제외하면, 24h가 지난 뒤 `greatest(…, now()-24h)`로 lo가 미끄러지면서 의미가 바뀝니다. 반대로 수집기 쿼리를 포함하면 "수집기가 남긴 로그가 heartbeat 역할"을 해서 우연히 동작합니다. 이 경우 정확성이 우연에 의존합니다.
- **수정안**: 진행 상태를 데이터에서 분리합니다.
  - RMV를 유지한다면 같은 SELECT에 sentinel 행을 `UNION ALL`로 붙입니다(`kind='progress'`, `window_hi`). watermark는 `max(window_hi) WHERE kind='progress'`에서 읽습니다. 원격 스트림이 실패하면 refresh 전체가 예외가 되므로, sentinel은 "끝까지 성공한 window"만 표시하게 됩니다. 단, UNION ALL 두 분기의 삽입 순서는 보장되지 않습니다. sentinel이 먼저 들어간 뒤 원격 분기가 실패하면 진행이 잘못 기록되므로 **실측이 필요**합니다.
  - Python 경로라면 `INSERT … SELECT`가 성공한 뒤 SQLite(또는 chDB 상태 테이블)에 `last_hi`를 커밋하면 끝입니다. 이쪽이 더 단순하고 정확하며, 제가 권하는 이유입니다.

### 🔴 1-2. 시계 혼용: `hi = now() - safety_lag`는 로컬 시계, `event_time`은 원격 시계
- **실패 시나리오**: 원격 서버(VM/컨테이너)의 시계가 로컬보다 20초 앞서 있다고 합시다. 원격 `event_time = T+20`인 행이 로컬 `hi = T-15` 기준으로는 미래라서 제외됩니다. watermark가 data-max 방식이면 다음 window의 lo에도 영향이 없어 결국 수집되긴 합니다. 반대 방향 skew(원격이 늦음)에서는 lag 15s가 사실상 줄어들어, flush 전 행을 건너뛰고 watermark가 그 위로 전진하면 **영구 누락**이 생깁니다.
- **수정안**:
  - 원격 시계 기준으로 상한을 잡습니다. 예: `hi = (SELECT now() FROM remoteSecure(nc, system.one)) - lag`. Python 경로라면 먼저 원격 `now()`를 조회해 파라미터로 넘깁니다.
  - 등록 probe에서 skew를 측정해서, 5초를 넘으면 경고를 표시합니다.

### 🔴 1-3. scalar subquery가 원격으로 전송될 위험 (실측 필요)
- **문제**: 의도는 "initiator(chDB)가 로컬 테이블에서 scalar를 계산해 상수로 접은 뒤 WHERE를 원격으로 push down"하는 것입니다. 분산 쿼리에서 scalar는 보통 initiator가 계산해서 넘기지만, analyzer 버전이나 쿼리 형태에 따라 두 가지 위험이 있습니다.
  - (a) subquery 텍스트가 그대로 원격에 전송되어 원격에서 `telemetry.query_log_raw`를 찾지 못하고 `UNKNOWN_TABLE`이 납니다.
  - (b) 필터가 push down되지 않아 원격 query_log 전체를 끌어온 뒤 로컬에서 거릅니다.
- **실패 시나리오**: (b)는 에러 없이 조용히 성능만 망가집니다. 15초마다 수백만 행이 전송됩니다.
- **수정안 (검증 게이트)**: RMV를 만든 뒤 원격 서버의 `system.query_log`에서 수집기 쿼리(`is_initial_query=0`)의 `query` 텍스트에 **상수 리터럴로 된 WHERE**가 있는지, 그리고 `read_rows`가 window 크기에 비례하는지 확인합니다. 둘 중 하나라도 어긋나면 Python 경로로 전환합니다.

### 🟠 1-4. "ORDER BY → prefix만 삽입" 가정은 취약
- **문제**: 부분 삽입이 prefix가 되려면 세 가지가 모두 성립해야 합니다. ① 원격이 정렬된 순서로 스트리밍하고, ② insert squashing이 순서를 보존하며, ③ 예외 발생 전까지의 블록이 커밋되어야 합니다. 어느 것도 계약으로 보장되지 않습니다.
- **실패 시나리오**: 블록이 커밋되는 경계가 정렬 경계와 어긋나서 prefix 대신 "중간이 빠진" 집합이 들어갑니다. data-max watermark는 그 위로 전진하므로 구멍이 영구화됩니다.
- **수정안**: 1-1의 명시적 진행 기록을 쓰면 부분 삽입은 "다음 window에서 overlap/dedup으로 재수집되는 잉여분"일 뿐입니다. prefix 성질에 의존할 필요가 없어집니다. ORDER BY는 아래 1-5의 청크 수집용으로만 남깁니다.

### 🟠 1-5. 재시작 직후 24h backfill 폭주
- **실패 시나리오**: 재오픈 시 모든 RMV가 즉시 1회 실행됩니다(실측). 바쁜 노드 3개가 각각 24h분 query_log 전체 컬럼(약 80개)을 동시에 끌어오면 메모리 사용이 치솟고 refresh가 장기화됩니다. 그동안 상태는 "수집 지연"으로 보이고 다음 refresh는 skip됩니다.
- **수정안**:
  - **필요한 컬럼만 SELECT**합니다(약 15개).
  - `ORDER BY event_time_microseconds LIMIT 100000`으로 청크 단위로 따라잡습니다. 진행 기록 `hi`는 청크의 마지막 `event_time`으로 합니다.
  - refresh 시작 시각을 분산시킵니다(`RANDOMIZE FOR`).

### 🟠 1-6. refresh hang에 상한 없음
- **실패 시나리오**: TLS 핸드셰이크 이후 원격이 응답하지 않으면, 기본 `receive_timeout`(300s) 동안 refresh가 붙잡혀 있습니다. 여기에 기본 재시도 3회와 RMV `refresh_retries`가 곱해져 수십 분 동안 "running" 상태가 지속됩니다. RMV background pool을 공유한다면 다른 노드 수집까지 굶길 수 있습니다(실측 필요).
- **수정안**: RMV SELECT에 `SETTINGS max_execution_time=30, receive_timeout=20, connect_timeout_with_failover_secure_ms=2000`를 지정하고, RMV `refresh_retries=0`으로 둡니다. 재시도는 다음 주기에 맡깁니다.

### 기타 사소한 지적
- watermark는 `event_time`(초 단위)보다 `event_time_microseconds`가 정확합니다. overlap 60s가 있어 실해는 작습니다.
- `source_host`에는 `hostName()` 대신 query_log의 `hostname` 컬럼을 쓰십시오. 원격/로컬 중 어디서 평가되는지 고민할 필요가 없습니다.
- `system.query_log`는 첫 flush 전에는 존재하지 않을 수 있습니다. 신규 서버에서 RMV CREATE가 519로 실패할 수 있으므로 아래 Q3의 "pending create" 상태가 필요합니다.

**권장 대안 요약**: query_log 수집은 Python 스케줄러(asyncio 태스크, 노드당 1개)가 맡습니다. 순서는 다음과 같습니다.
1. 원격 `now()`를 조회합니다.
2. `INSERT INTO raw SELECT <cols> FROM remoteSecure(nc_x, system, query_log) WHERE event_time_microseconds > {lo:DateTime64(6)} AND event_time <= {hi:DateTime} ORDER BY … LIMIT {n:UInt32}`를 실행합니다.
3. 성공하면 진행 상태를 커밋합니다.

RMV와 같은 SQL이고 엔진 스케줄러만 Python으로 바뀝니다. "원안 = RMV"를 고수해야 한다면 metrics는 RMV로 두고 query_log만 검증 게이트 결과에 따라 분기하는 하이브리드가 합리적인 타협입니다.

---

## Q2. DDL 안전 생성과 quoting

### 🟡 2-1. host는 애초에 DDL에 넣지 마십시오
- 설계대로 RMV DDL에는 **서버가 생성한 hex 식별자만** 들어가야 합니다. cid/nid도 hex이므로 `'a3f9…'` 리터럴 자체가 안전합니다.
- host, port, user, password는 **NC 하나에만** 들어갑니다. 정규식 검증 규칙은 다음과 같습니다.
  - 식별자: `^[a-z][a-z0-9_]{0,62}$`, 서버에서 생성합니다.
  - cid/nid 리터럴: `^[0-9a-f]{16}$`
  - host: `^[A-Za-z0-9.-]{1,253}$` 또는 IPv4/IPv6. IPv6는 `[]`와 `:` 처리를 별도로 합니다.
  - port: 1–65535
  - user: `^[A-Za-z0-9_.@-]{1,64}$`
- 템플릿은 `str.format`이 아니라, 검증 함수를 통과한 값만 받는 작은 빌더 함수 하나로 고정합니다. 이 빌더에 대한 단위 테스트가 Q2의 실질적인 안전장치입니다.

### 🟡 2-2. `CREATE NAMED COLLECTION`은 password가 DDL 리터럴로 들어가는 유일한 지점
- **문제**: NC 생성 DDL은 parameter binding을 쓸 수 없을 가능성이 높습니다(실측 필요). password는 임의 문자열이라 정규식으로 제한할 수도 없습니다.
- **실패 시나리오**:
  - ① 이스케이프 결함으로 `'`나 `\`를 포함한 password가 DDL을 깨뜨립니다.
  - ② chDB 예외 메시지에 원본 쿼리 텍스트가 포함되고, Python 예외가 FastAPI/uvicorn 로그와 500 응답 본문에 그대로 찍힙니다.
  - ③ chDB 로컬 `system.query_log`/`text_log`에 DDL이 남습니다. ClickHouse에는 NC/remote secret masking이 있지만 chDB 빌드에서 적용되는지는 미확인입니다.
- **수정안**:
  - 문자열 리터럴 quoting은 `\` → `\\`, `'` → `\'` 두 규칙으로 고정합니다. NUL과 제어문자는 거부합니다(사용자에게 "지원하지 않는 문자"로 안내). 유니코드는 허용하되 UTF-8 유효성을 확인합니다.
  - **round-trip 테스트**를 작성합니다. `'`, `\`, `\'`, `''`, `--`, `;`, 이모지, 1KB 길이 password로 NC를 만든 뒤 그 NC로 실제 원격 로그인에 성공하는지 확인합니다.
  - NC DDL 실행부는 `try/except`로 감싸서 예외를 **에러코드와 고정 메시지로 치환**하고, 원본 메시지는 버립니다. 로그에는 남기지 않습니다.
  - 로컬 chDB의 `query_log`/`text_log`를 config-file에서 비활성화하거나, 실측으로 masking을 확인합니다.
  - 검증 테스트로 `grep -r '<테스트 password>' <data dir> ~/.config/mini-loghouse <로그>` 결과가 0건이어야 합니다. 이 테스트를 CI 체크리스트에 넣으십시오.

### 🟡 2-3. probe에서 "parameter binding으로 password 전달"도 실측 필요
- table function 인자 안에서 `{pw:String}`이 바인딩되는지는 버전 의존입니다. 바인딩이 안 되면 결국 리터럴 quoting으로 회귀합니다.
- **수정안**: probe는 아래 Q3-1처럼 **subprocess**로 실행하고, password는 stdin으로 전달합니다. subprocess 안에서는 2-2와 같은 quoting 빌더를 쓰되 임시 data dir을 사용하고 종료 시 삭제합니다. 이렇게 하면 probe가 실제 수집 경로(chDB의 remoteSecure + TLS 설정)를 그대로 검증한다는 장점도 생깁니다.

### 🔴 2-4. TLS 설정은 NC 단위가 아닙니다
- **문제**: "등록 시 TLS 입력"이라는 요구가 있지만, `remoteSecure`의 인증서 검증(CA, verificationMode)은 chDB config-file의 **전역** `openSSL.client` 설정입니다. 클러스터별 CA나 클러스터별 "검증 안 함"은 불가능할 가능성이 큽니다.
- **실패 시나리오**: A는 사설 CA, B는 공인 인증서를 쓰는 상황입니다. A를 위해 전역 `verificationMode=none`을 켜면 B의 검증까지 꺼집니다.
- **수정안**: PoC의 TLS 옵션을 "시스템 trust store + 앱 전역 추가 CA 번들 1개(append)"로 제한하고 UI에 그렇게 명시합니다. 등록 화면의 TLS 입력은 "secure 여부"와 "CA 파일 추가(전역에 반영, 재시작 필요)"로 정직하게 표현합니다.

---

## Q3. 소유권, 동시성, 키 관리에서 빠진 실패 모드

### 🟠 3-1. "별도 Session으로 probe"는 chDB에서 성립하지 않을 수 있음
- **문제**: chDB는 프로세스당 엔진 컨텍스트가 하나입니다. 버전에 따라 "서로 다른 path로 두 번째 Session을 열 수 없음" 또는 "새 Session이 기존 Session을 닫음" 같은 제약이 있습니다(4.4에서 실측 필요).
- **실패 시나리오**: 연결 테스트 버튼을 누르면 수집용 Session이 닫히거나 예외가 납니다. 혹은 같은 Session에서 thread로 돌리다가 원격 timeout 동안 Dashboard 쿼리가 블로킹됩니다.
- **수정안**: probe는 **subprocess**(`python -m mini_loghouse.probe`)로 실행합니다. 임시 dir을 쓰고, password는 stdin으로 넘기고, 하드 타임아웃이 지나면 kill합니다. 동시 실행은 최대 2개로 제한합니다. 프로세스 격리 덕분에 hang, 크래시, 메모리 문제가 본 프로세스에 전파되지 않습니다.

### 🟠 3-2. 단일 Session에 대한 동시 접근
- **문제**: FastAPI의 sync 핸들러는 threadpool에서 돕니다. Dashboard 쿼리 여러 개, supervisor 폴링, (Python 경로라면) 수집 INSERT가 같은 Session을 동시에 호출하게 됩니다. chDB Python 바인딩의 동시 호출 안전성은 버전 의존입니다.
- **수정안**: `CollectorOwner`가 Session 호출을 **하나의 lock(또는 전용 worker thread + queue)** 으로 직렬화합니다. Dashboard 쿼리에는 `max_execution_time=10`을 걸고, 수집 INSERT는 청크로 짧게 나눠서 Dashboard가 몇 초 이상 막히지 않게 합니다. RMV 자체는 엔진 내부 스레드라 이 lock 밖에서 돕니다.

### 🟠 3-3. fcntl lock의 POSIX 함정
- **문제**: `fcntl.lockf`/`F_SETLK` 레코드 lock은 **같은 프로세스에서 해당 파일의 어떤 fd든 close되면 해제**됩니다.
- **실패 시나리오**: 어딘가에서 lock 파일을 읽고 닫는 순간 lock이 풀리고, 두 번째 인스턴스가 같은 data dir로 chDB를 엽니다. 그 결과 MergeTree가 손상됩니다.
- **수정안**:
  - `fcntl.flock`(BSD lock, fd 단위)을 사용합니다.
  - lock fd는 모듈 전역에 유지하고, 그 파일은 다른 곳에서 절대 열지 않습니다.
  - **chDB open 전에** lock을 획득합니다.
  - PID와 시작 시각을 기록해서 "이미 실행 중: pid N" 메시지를 보여줍니다.

### 🟠 3-4. SQLite와 chDB 사이의 원자성 부재 → 고아/유령 리소스
- **실패 시나리오**: 등록 흐름은 SQLite insert → NC 생성 → RMV 생성 순서입니다. NC 생성 직후 크래시가 나면 SQLite에는 행이 없거나 미완료 상태로 남고, chDB에는 password를 담은 NC가 고아로 남습니다. 반대로 RMV는 있는데 NC가 없는 경우도 생길 수 있습니다.
- **수정안**:
  - SQLite에 desired state와 `phase ∈ {provisioning, active, pending_create, deleting}`을 둡니다.
  - 시작 시 reconcile 단계에서 `system.named_collections`와 `system.tables`의 `nc_*`/`mv_*`를 SQLite와 대조합니다.
    - SQLite에 없는 chDB 리소스는 DROP합니다.
    - RMV가 없고 NC만 있으면 RMV를 재생성합니다.
    - **NC가 없으면 password 재입력 상태**로 둡니다. password의 유일한 사본이 NC이기 때문입니다.
  - 삭제 순서는 RMV DROP → NC DROP → SQLite 삭제입니다.

### 🟠 3-5. 등록 시점에 노드가 다운이면 RMV를 만들 수 없음 (519)
- **수정안**: `pending_create` phase를 두고, supervisor가 지수 backoff로 재시도합니다. 상태는 "준비 중(노드 응답 없음)"으로 표시합니다. 원격 서버 업그레이드로 query_log 스키마가 바뀌는 경우에 대비해 명시적 컬럼만 SELECT합니다.

### 🟠 3-6. 키 관리
- **키 자동 생성 금지 조건**: "키 파일이 없으면 생성"하는 로직은, 키를 잃어버렸는데 암호화된 NC가 남아 있는 상황에서 새 키를 만듭니다. 그러면 chDB **전체 open이 실패**하고(실측), telemetry 전체가 접근 불가가 됩니다.
  - **수정**: SQLite에 키 fingerprint(`sha256(key)[:16]`)를 저장합니다. open 전에 대조하고, 불일치하거나 키가 없는데 `named_collections/`가 비어 있지 않으면 자동 생성하지 말고 다음 순서로 복구합니다.
    1. `named_collections/`를 `quarantine-<ts>/`로 이동합니다.
    2. 새 키를 만들고 chDB를 엽니다.
    3. 모든 클러스터를 "재입력 필요"로 전환합니다.
  - 이 흐름에서 **NC 없이도 RMV 정의가 로드되어 chDB가 열리는지**는 실측이 필요합니다. 열리지 않는다면 RMV도 DROP 대상에 넣어야 합니다.
- **env 경로의 부작용**: `key_hex from_env`를 쓰려면 앱이 파일에서 키를 읽어 `os.environ`에 넣어야 합니다. 그러면 3-1의 probe subprocess를 포함한 모든 자식 프로세스에 키가 상속됩니다. **수정**: subprocess는 `env=`를 명시해서 키 변수를 제거합니다. 운영자 env와 파일 두 경로를 다 지원하지 말고 **파일 하나만** 지원합니다.
- **키 파일 권한**: 생성 시 `os.open(..., O_CREAT|O_EXCL, 0o600)`을 씁니다. 기존 파일의 mode가 0600보다 넓으면 기동을 거부합니다. `~/.config/mini-loghouse/`는 0700으로 둡니다.

### 🟠 3-7. 종료와 sleep
- **SIGTERM**: uvicorn lifespan shutdown에서 Session을 명시적으로 close합니다. 그렇지 않으면 다음 open에서 복구 비용이 생기거나 부분 part가 남습니다.
- **metrics snapshot의 부분 삽입**: 종료나 장애가 refresh 도중에 일어나면 snapshot이 일부만 들어갑니다. **수정**: Dashboard의 "현재 metric"은 `collected_at`별 행 수가 최빈값 이상인 마지막 snapshot만 사용합니다.
- **macOS sleep/wake**: sleep 중에는 수집이 멈추고, wake 후에는 query_log backfill로 회복됩니다. metrics는 공백으로 남습니다. 시계열 차트에서 공백을 선으로 잇지 말고 끊어서 그리십시오.

### 🔴 3-8. TTL 부재 → 무한 성장
- 15초 간격으로 노드당 metric 수백 개, 노드 3개를 수집합니다. 하루 수백만 행이 쌓이고 디스크가 단조 증가합니다. **수정**: `metrics_raw TTL collected_at + INTERVAL 3 DAY`, `query_log_raw TTL event_time + INTERVAL 7 DAY`. Dashboard 최대 기간이 24h이므로 충분합니다.

---

## Q4. 집계 정의 (7번)

### 🔴 4-1. 수집기 자신의 쿼리 처리
- **문제**: `remote()`로 보낸 쿼리는 원격에서 **`is_initial_query=0`**(secondary)으로 기록됩니다. 그래서 "논리 요청"에서는 이미 빠지지만, "노드별 실행"에는 15초마다 노드당 2건(metrics + query_log)씩 계속 섞입니다. `log_comment`가 원격까지 전파되는지는 RMV 경로에서 실측이 필요합니다.
- **실패 시나리오**: 유휴 클러스터인데도 "노드별 실행 480건/h"가 표시됩니다.
- **수정안**:
  - 모든 수집 SQL에 `SETTINGS log_comment='mlh-<설치별 랜덤 hex>'`를 붙여서, 사용자가 같은 문자열을 쓰는 우연한 충돌을 피합니다.
  - 원격 query_log에서 해당 값이 보이는지 검증합니다.
  - 제외는 수집 단계(`WHERE log_comment != …`)에서 합니다. 진행 기록을 분리했으므로(1-1) 수집 단계에서 제외해도 watermark 문제가 없습니다.

### 🔴 4-2. secondary 쿼리 = 외부 remote()와 Distributed 하위 쿼리
- `is_initial_query=0`인 쿼리에는 이 클러스터의 Distributed fan-out뿐 아니라, **다른 ClickHouse가 remote()로 이 서버에 보낸 쿼리**도 포함됩니다. 후자는 이 클러스터 입장에서 "외부 요청"인데 논리 요청에서 빠집니다.
- PoC에서는 정의를 이렇게 명시하는 것으로 충분합니다: "논리 요청 = 이 클러스터 노드가 initiator인 쿼리. 커버리지가 부분이면 미수집 노드에서 시작된 요청은 누락됨." UI의 노드 커버리지 배지 옆에 이 의미를 툴팁으로 보여주십시오.

### 🟠 4-3. 재시도와 query_id 재사용
- 클라이언트 재시도는 대부분 새 query_id를 만듭니다. 그래서 "논리 요청 수"는 재시도를 별개 요청으로 셉니다. 이것은 의도된 동작이므로 정의에 명시합니다.
- 클라이언트가 지정한 query_id를 순차적으로 재사용하는 경우도 있습니다. dedup key에 `event_time_microseconds`가 있어서 두 실행이 모두 보존되므로 **올바릅니다**. 반대로 `uniqExact(query_id)`로 세면 잘못됩니다. 집계는 반드시 행 단위 count(FINAL 후)로 하십시오.

### 🟠 4-4. 실패 정의의 경계
- `ExceptionWhileProcessing`에는 사용자 `KILL QUERY`/취소(code 394)와 `max_execution_time` 초과(159)가 포함됩니다. 실패율이 운영자 행동으로 부풀 수 있습니다. **수정**: 실패 수는 그대로 세되, 오류 테이블에 `exception_code`를 노출하고 394를 "취소"로 구분 표시합니다.
- **인증 실패는 query_log에 남지 않습니다**(session_log 영역). "실패 수 0 = 문제없음"으로 오해하지 않게 범위를 명시하십시오.

### 🟠 4-5. p95와 중복
- ReplacingMergeTree는 merge 전까지 중복을 유지합니다. overlap 60s 때문에 매 refresh마다 중복이 생깁니다. **FINAL 누락 = 카운트 1.x배 부풀림**입니다. 모든 Dashboard 쿼리는 하나의 `v_query_log` 뷰(`SELECT … FROM query_log_raw FINAL WHERE log_comment != …`)를 경유하도록 강제하십시오. 집계 정의를 뷰 하나에 모으는 것이 실수 방지에 가장 효과적입니다.
- `quantile`은 근사값입니다. PoC 데이터 규모에서는 `quantileExact`로 충분하며, 정확한 값이 테스트 검증을 쉽게 합니다.
- p95가 성공 쿼리만 대상인지 UI 라벨에 명시하십시오("p95 (성공 쿼리)").

### 🟠 4-6. 시간대
- 시간대별 추이(`toStartOfHour`)는 chDB 세션 타임존을 기준으로 계산됩니다. 원격 서버 타임존과 브라우저 타임존이 다르면 버킷 경계가 어긋납니다. **수정**: 저장과 집계는 UTC로 하고, 응답은 epoch ms로 보냅니다. 렌더링 시 브라우저 로컬 시간으로 변환합니다.

---

## 4번 노드 발견 (질문에는 없지만 가장 먼저 깨질 곳)

### 🔴 D-1. `system.clusters`의 host/port는 클러스터 내부 관점 값
- **실패 시나리오**:
  - docker compose의 A(2노드) 환경에서 `host_name = clickhouse-a2`입니다. 노트북에서는 DNS 오류(198)가 납니다.
  - `port`는 interserver TCP 9000인데 remoteSecure에는 9440(또는 매핑된 포트)이 필요합니다.
  - 그 결과 커버리지가 "일부(1/2)"로 **틀리게** 표시됩니다.
  - `system.clusters`에는 사용자 정의 클러스터 외에 기본 예제 클러스터(`default`, `test_shard_localhost` 등)도 섞여 있어서, "발견 노드 수" 자체가 오염됩니다.
- **수정안**:
  - 등록 2단계 UI를 둡니다. 클러스터를 선택하게 하고, 노드별 "접속 주소(host:port)" 편집 필드를 제공합니다. 기본값은 발견된 host와 secure port 추정값입니다.
  - 노드의 정체성은 주소가 아니라 probe로 얻은 `hostName()` + `serverUUID()`로 판정합니다. 등록 endpoint와 발견 노드가 같은 서버이면 중복 수집을 막습니다.
  - 커버리지 분모는 "사용자가 확정한 노드 수"로 합니다.

---

## Q5. 과잉설계(빼기)와 필수 추가

### ⚪ 빼야 할 것
| 항목 | 이유 / 대체 |
|---|---|
| double-submit CSRF 토큰 | loopback 단일 운영자 환경에서는 **Origin 검사 + `Content-Type: application/json` 강제 + 커스텀 헤더 요구**로 충분합니다. 이 조합만으로 cross-site form/simple request가 막힙니다. |
| 등록 idempotency key | `(host, port, user)` unique 제약과 버튼 disable로 충분합니다. |
| `collection_status` 이력 append | 재시작 후 "마지막 성공 시각"은 `max(collected_at)`(metrics)와 진행 기록(query_log)에서 그대로 나옵니다. 실데이터에서 파생하면 상태와 데이터가 어긋날 수 없습니다. `view_refreshes`는 **현재 오류 메시지와 다음 refresh 시각**용으로만 실시간 조회합니다. 테이블 하나와 폴링 루프 하나가 줄어듭니다. |
| 키 env 경로 | 파일 하나만 지원합니다(3-6). |
| 노드 필터 × 3개 기간 × 모든 패널 | PoC에서는 노드 필터를 시계열과 집계 카드에만 적용해도 됩니다. 쿼리 테이블은 노드 컬럼 표시로 대체할 수 있습니다. |

### 🟡 반드시 추가할 것
1. **Host 헤더 검증 (DNS rebinding 방어)**: 127.0.0.1에 bind해도 악성 페이지가 `evil.com → 127.0.0.1` rebinding으로 same-origin GET을 보낼 수 있습니다. 그러면 Dashboard 데이터(쿼리 텍스트, 사용자명)가 유출됩니다. `Host ∈ {127.0.0.1:PORT, localhost:PORT}`가 아니면 421을 반환합니다. Origin 검사는 POST만 보호하므로 이것이 GET을 보호하는 유일한 장치입니다.
2. **쿼리 텍스트는 민감 데이터입니다**: query_log의 `query`에는 다른 사용자의 `CREATE USER … IDENTIFIED BY`나 remote() 리터럴 password가 들어 있을 수 있습니다. 서버측 masking이 대부분 처리하지만, 표시 길이를 제한하고 단일 운영자 전용임을 명시하십시오. data dir 0700은 이미 있으니 유지합니다.
3. **예외 sanitizer**: chDB, uvicorn, FastAPI의 모든 예외 경로가 password를 포함한 쿼리 텍스트를 응답이나 로그로 내보내지 않도록 합니다. 2-2의 grep 테스트로 검증합니다.
4. **TTL** (3-8), **RMV timeout 설정** (1-6), **pending_create 재시도** (3-5), **기동 reconcile** (3-4).
5. **클러스터 삭제/일시정지**: 테스트 반복 중 가장 먼저 필요해지는 기능입니다. 삭제가 없으면 고아 NC가 쌓입니다.
6. **진단 페이지 하나**: 노드별 마지막 window(lo/hi), 수집 행 수, 원격 시계 skew, 마지막 오류코드를 보여줍니다. 상태 판정 로직 디버깅 시간을 크게 줄여줍니다.

### 상태 정의 보완
- 우선순위를 명시하십시오: **오류 > 지연 > 수집 중 > 첫 수집 대기 > 준비 중**.
- 클러스터 상태 = 노드 상태 중 최악값으로 하되, 커버리지 배지를 별도로 둡니다. 그래야 "1개 노드 오류 + 나머지 정상"이 클러스터 전체 오류로 오인되지 않습니다.
- "최근 refresh exception = 오류"는 일시적 네트워크 블립 하나에도 오류를 띄웁니다. **연속 2회 실패부터 오류**로 판정하고, 1회 실패는 "수집 중(경고)"으로 표시하십시오.

---

## 새로운 관점: "RMV 원안"을 어떻게 판정할 것인가

이 설계의 가장 큰 리스크는 개별 버그가 아니라 **검증 순서**입니다. 현재 "서버 확보 후 실측 예정"인 항목들은 다음과 같습니다.
- NC 참조 RMV의 동작
- scalar 전송 여부
- log_comment 전파
- 다중 Session 제약
- NC 없는 RMV 로드

이들은 전부 아키텍처 분기점인데, UI나 상태 머신보다 뒤에 검증될 예정입니다. 제안은 다음과 같습니다.

> **Day 0 스파이크 (반나절)**: ClickHouse 26.8 단일 노드 하나를 띄우고, 위 5개 항목과 1-3의 push-down 확인, 2-2의 password round-trip을 스크립트 하나로 검증합니다. 결과를 표로 남기고 query_log 수집 경로(RMV vs Python)를 확정한 다음 나머지를 구현합니다.

Python 경로로 결정되더라도 손실은 거의 없습니다. SQL과 테이블 스키마, 집계 뷰는 동일하고, 바뀌는 것은 스케줄러 약 60줄입니다. 반대로 RMV 경로로 다 만든 뒤에 1-1이나 1-3을 발견하면 상태 판정, 복구, 진단 로직을 전부 다시 짜야 합니다.

### 전문가 C — Codex/runtime-default/runtime-default
제 의견은 **metrics에는 RMV를 유지하되, query_log 증분 수집은 Python의 명시적 수집 루프로 옮기는 것**입니다. 지금 초안의 가장 큰 결함은 중복 자체가 아니라, **“저장된 이벤트의 최대 시각”을 “그 이전 구간을 빠짐없이 수집했다는 증거”로 취급하는 것**입니다. `ORDER BY`와 overlap만으로 이 간극을 메울 수 없습니다.

반대로 단일 프로세스, 단일 Session 소유자, 로컬 저장 데이터만 읽는 Dashboard, 노드별 장애 격리는 PoC에 잘 맞습니다. 수정의 중심은 기능 추가보다 **수집 완료의 정의와 복구 경계의 단순화**여야 합니다.

아래에서 실측된 사실과 아직 확인해야 할 엔진 동작을 구분하겠습니다.

### 1. Q1 — watermark와 ORDER BY는 현재 형태로 정확성을 보장하지 못합니다

**문제 →** `max(event_time)`은 관측한 행의 최대 시각이지, 성공적으로 처리한 구간의 끝이 아닙니다.

**실패 시나리오 →** 원격 SELECT에서 일부 블록이 먼저 저장된 뒤 연결이 끊깁니다. 저장된 행 중 시각이 큰 행이 하나라도 있으면 다음 refresh의 watermark가 앞으로 이동합니다. 누락된 행이 overlap보다 오래되면 영구적으로 빠집니다. 특히 늦게 flush된 query_log는 수집 실패가 없어도 같은 문제가 생깁니다.

`ORDER BY event_time_microseconds`는 충분한 해결책이 아닙니다.

- 최종 결과의 정렬과 원격 읽기·블록 전송·대상 삽입의 실패 원자성은 서로 다른 성질입니다.
- 정렬이 로컬에서 수행되는지 원격에 내려가는지에 따라 실행과 메모리 비용이 달라집니다.
- 결과가 완전히 정렬되더라도, 나중에 원격 로그에 나타나는 과거 이벤트까지 해결하지 못합니다.
- APPEND refresh 실패 때 이미 삽입한 블록이 남는지는 해당 버전에서 장애 주입으로 확인해야 합니다.

**수정안 →** 별도의 `committed_hi`를 두고 **해당 구간의 원격 읽기와 로컬 저장이 모두 성공한 뒤에만** 전진시키십시오.

Python 수집 루프라면 다음 정도로 충분합니다.

1. 노드마다 `[committed_hi - overlap, hi)`를 계산합니다.
2. 원격 조회 결과를 스트리밍하며 로컬에 저장합니다.
3. 조회 EOF와 모든 로컬 쓰기의 성공을 확인합니다.
4. 마지막에 SQLite의 `committed_hi`를 갱신합니다.
5. 중간에 죽으면 같은 구간을 다시 읽고 dedup합니다.

telemetry 저장과 SQLite cursor 갱신 사이에 원자적 트랜잭션이 없어도, **저장 먼저, cursor 나중**이면 재실행에 따른 중복으로 수렴합니다. 반대 순서는 누락을 만듭니다. 전제는 chDB 쓰기 완료·영속화의 의미를 확인하는 것입니다.

이 방식도 무제한 지연 도착까지 보장하지는 않습니다. 보장 범위는 “원격 보존 기간 안에서, 설정한 재조회 범위에 들어온 이벤트”입니다.

---

**문제 →** scalar subquery를 썼다는 사실만으로 시간 조건이 원격 서버에서 효율적으로 실행된다고 볼 수 없습니다.

**실패 시나리오 →** 로컬 watermark 조회가 들어간 조건이 원격으로 충분히 내려가지 않아 매 refresh마다 큰 `system.query_log`를 읽습니다. 로컬 `ORDER BY`까지 붙으면 15초 주기를 넘기고 메모리 사용이 커집니다. 타입·타임존·정밀도 차이로 경계 처리가 예상과 달라질 수도 있습니다.

**수정안 →** 원격 조회에는 이미 계산한 UTC 경계 상수를 넘기고, **원격 서버에서 시간 필터가 적용되는 SQL**을 실행하십시오. 비교는 한 가지 정밀도로 통일하고 `[lo, hi)`를 사용합니다.

RMV를 유지한다면 최소한 실제 생성 SQL과 원격 `system.query_log`에서 실행 쿼리·읽은 행 수를 확인해야 합니다. 작은 데이터에서 결과가 맞는 것만으로 predicate pushdown을 입증할 수는 없습니다.

---

**문제 →** 빈 구간에서는 `max(event_time)`이 전진하지 않습니다. `max_backfill` 절단은 손실을 정상 복구처럼 보이게 할 수 있습니다.

**실패 시나리오 →** 사용량이 적은 노드는 같은 과거 구간을 계속 읽습니다. 반대로 30시간 정지 후 재시작하면 최근 24시간만 가져오면서 빠진 6시간이 UI에 드러나지 않습니다.

**수정안 →** 이벤트 유무와 무관하게 성공한 구간의 `hi`를 저장하십시오. backfill 제한으로 건너뛴 구간은 `gap_from`, `gap_to`, 사유로 남기고 Dashboard에 표시합니다. 쿼리 수가 0인 것과 수집하지 못한 것은 구별해야 합니다.

---

**문제 →** `safety_lag=15s > flush_interval=7.5s`는 완전성 조건이 아닙니다.

**실패 시나리오 →** 부하·로그 큐 적체·일시적 오류로 flush가 60초 이상 늦어집니다. 이벤트 시각이 이미 overlap 밖이면 정상 실행된 refresh에서도 누락됩니다.

**수정안 →** 15초는 지연 완화 설정으로 취급하고, overlap은 실제 지연 분포에 맞춰 잡습니다. PoC에서는 더 넓은 최근 구간 재조회가 합리적입니다. 필요하면 낮은 빈도의 더 긴 재조회로 보완하되, 비용과 원격 로그 보존 기간을 명시하십시오.

**RMV만 사용해야 한다면**, watermark를 없애고 매번 고정된 최근 구간을 재조회한 뒤 dedup하는 편이 더 단순하고 정직합니다. 대신 긴 다운타임 복구는 별도의 명시적 backfill 작업으로 처리해야 합니다.

### 2. Q2 — DDL은 입력을 줄여서 안전하게 만드는 것이 우선입니다

**문제 →** parameter binding은 SQL injection 방지 수단이지만, 비밀의 저장·노출 방지 수단은 아닙니다.

**실패 시나리오 →** probe에 바인딩한 password가 클라이언트 디버그 로그, query parameter 기록, 예외 객체, 진단 덤프에 남습니다. 암호화된 NC를 사용해도 생성 SQL이나 애플리케이션 로깅에서 이미 유출될 수 있습니다.

**수정안 →** probe와 NC 생성 경로 모두 비밀 취급 대상으로 지정하십시오. 요청 body·SQL·params·원본 예외를 그대로 기록하지 말고, 사용자에게는 분류된 오류와 비밀을 제거한 요약만 반환합니다. “실측 오류 메시지에 password가 없었다”는 결과는 유용하지만 모든 경로의 보장은 아닙니다.

---

**문제 →** identifier와 문자열 리터럴을 같은 quoting 정책으로 처리하면 취약해집니다.

**실패 시나리오 →** 사용자 host를 SQL 조각으로 조립하거나, NC 이름에 일반 문자열 escaping을 적용하면서 구문 경계가 깨집니다.

**수정안 →** 생성 규칙을 세 가지로 고정하십시오.

| 종류 | 정책 |
|---|---|
| DB·테이블·MV·NC identifier | 서버 생성 ASCII 이름만 허용. 예: `nc_` + 고정 길이 소문자 hex |
| cid/nid | 서버 생성 hex를 검증한 뒤 고정 템플릿에 삽입 |
| host/user/password 등 문자열 | 지원되는 위치에서는 binding. 불가능하면 단일 중앙 serializer 사용 |

serializer는 ClickHouse 문자열 리터럴 규칙에 맞춰 **작은따옴표와 백슬래시를 모두 처리**해야 합니다. NUL·제어문자는 거부하고 Unicode 정책도 정하십시오. 일반 SQL용으로 작은따옴표만 두 번 쓰는 함수를 임의로 가져오지 않는 편이 좋습니다.

가장 좋은 구조는 RMV DDL에 사용자 문자열을 넣지 않고, 검증된 NC identifier와 내부 ID만 넣는 것입니다. host는 IPv4·IPv6·DNS를 실제 파서로 구분하고, port·TLS를 별도 필드로 보관하십시오. 주소 문자열에 포트나 table-function 문법을 사용자가 섞어 넣게 하지 않아야 합니다.

### 3. Q3 — Session 소유권은 선언보다 실행 경로가 중요합니다

**문제 →** “Session 하나를 소유한다”와 “Session 접근이 직렬화된다”는 다릅니다.

**실패 시나리오 →** FastAPI 요청, 상태 supervisor, 등록 DDL, 종료 작업이 같은 Session을 동시에 호출합니다. 별도 thread의 probe가 실제로 같은 내부 엔진 자원을 공유하거나, timeout 응답 후에도 실행을 계속해 수집을 방해합니다.

**수정안 →** Session 호출을 전담 executor/작업 큐 하나로 모으고 HTTP handler가 직접 호출하지 않도록 하십시오. 대시보드 쿼리에도 기간·행 수·실행 시간 제한이 필요합니다.

별도 Session의 격리와 취소 동작은 실측해야 합니다. Python의 대기 timeout만으로 실행 중인 native 쿼리가 취소되지는 않습니다. 강제 종료까지 필요한 probe라면 별도 프로세스가 더 명확한 격리 경계입니다.

---

**문제 →** SQLite metadata, NC, RMV는 하나의 트랜잭션으로 생성되지 않습니다.

**실패 시나리오 →** SQLite 등록 직후 죽으면 수집 객체가 없습니다. NC 생성 뒤 죽으면 고아 자격증명이 남습니다. RMV 생성 중 원격 서버가 내려가면 일부 노드만 구성됩니다.

**수정안 →** 등록을 작은 상태 머신으로 만드십시오. `pending → provisioning → active / needs_attention` 정도면 충분합니다. 내부 ID로 객체 이름을 결정적으로 만들고 reconcile에서 누락된 객체를 재생성합니다. 원격 장애로 실패한 DDL도 backend가 재시도해야 합니다.

삭제도 먼저 수집을 멈춘 뒤 NC를 제거해야 합니다. 재시도 횟수와 마지막 provisioning 오류를 남기되, 복잡한 범용 workflow 엔진은 필요 없습니다.

---

**문제 →** 키를 data dir 밖에 둔 것은 파일 분리이지 강한 보안 경계는 아닙니다.

**실패 시나리오 →** 같은 사용자 권한으로 실행되는 악성 프로세스는 키와 데이터를 모두 읽습니다. 키만 유실되면 실측대로 chDB 전체가 열리지 않아 telemetry 조회까지 막힙니다. 최초 키 생성 도중 충돌하거나 환경변수 값이 바뀌어도 같은 상황이 됩니다.

**수정안 →** 보호 목표를 “키 없는 데이터 디렉터리 유출에 대한 보호”로 명시하십시오. 키 생성은 배타 생성·0600·원자적 저장으로 처리하고 부모 디렉터리 권한도 제한합니다. 키 백업과 복구 정책을 함께 정해야 합니다.

키 오류에서는 키를 자동 재생성하지 말고 backend를 복구 모드로 띄우십시오. NC 격리는 엔진이 닫힌 상태에서 수행하고 원본을 보존해야 합니다. **키 유실 복구 때문에 기존 telemetry를 지우는 흐름은 피해야 합니다.**

또한 lock은 chDB open보다 먼저 잡고 프로세스 수명 동안 FD를 유지해야 합니다. lock 파일을 실행 중에 지우거나 교체하지 않는 규칙도 필요합니다.

### 4. topology와 loopback 보안에 빠진 가정이 있습니다

**문제 →** `system.clusters`는 등록 endpoint가 속한 클러스터의 확정적 membership 목록이 아닙니다.

**실패 시나리오 →** 서버에는 여러 클러스터 정의와 alias가 있습니다. 임의로 전부 펼치면 관련 없는 노드까지 수집하거나 동일 서버를 중복 수집합니다. 반대로 localhost·내부 DNS 주소는 앱에서 접근할 수 없습니다. endpoint가 LB라면 같은 node_id로 다른 물리 노드의 로그가 섞일 수도 있습니다.

**수정안 →** 발견 결과를 “후보”로 취급하고 선택한 클러스터 정의 또는 명시적 노드 목록을 확정하십시오. PoC에는 수동 노드 목록이 더 예측 가능합니다. 커버리지 분모는 그 확정 목록이어야 합니다.

노드 식별자는 주소 alias와 분리하고, 관측 가능한 서버 식별 정보로 중복을 점검하십시오. `hostName()`만으로 전역 유일성을 가정해서는 안 됩니다. 자동 발견 실패는 단일 endpoint 수집으로 명확히 표시합니다.

---

**문제 →** 127.0.0.1 bind와 POST Origin 검사만으로 로컬 웹앱의 접근 경계가 완성되지는 않습니다.

**실패 시나리오 →** DNS rebinding으로 예상하지 않은 Host를 가진 요청이 loopback에 도착합니다. CORS 설정이 느슨하면 응답을 읽을 수 있습니다. 수집된 SQL 자체가 password·토큰·개인정보를 포함할 수도 있습니다.

**수정안 →** `Host`와 `Origin`을 고정된 loopback origin allowlist로 검증하고 CORS를 열지 마십시오. 상태 변경은 JSON과 CSRF 검증을 요구하고 GET으로 수행하지 않습니다. CSRF cookie에는 적절한 SameSite 설정이 필요합니다.

더 중요한 누락은 **query_log의 쿼리 원문도 비밀 데이터라는 점**입니다. 원문 저장·표시 정책과 보존 기간을 정하고, 필요하면 기본 화면에서는 원문을 축약하거나 숨기십시오. NC 암호화는 telemetry 파일의 SQL 원문까지 보호하지 않습니다.

### 5. Q4 — 집계는 대체로 타당하지만 이름과 dedup 의미를 바꿔야 합니다

**문제 →** initial terminal event 수를 “논리 요청 수”라고 부르면 재시도와 분산 실행의 의미를 과장합니다.

**실패 시나리오 →** 클라이언트가 한 사용자 동작을 세 번 재시도하면 세 개의 initial query가 기록됩니다. 동일 query_id를 재사용할 수도 있습니다. 분산 환경의 내부 실행도 단순히 “노드당 한 번”으로 대응하지 않습니다.

**수정안 →** 화면 이름을 **“기록된 initial 쿼리 완료·실패 수”** 또는 설명이 붙은 “쿼리 실행 수”로 바꾸십시오. 사용자 수준 논리 요청 수는 애플리케이션 request ID와 재시도 정책 없이는 복원할 수 없습니다.

`is_initial_query=1`과 terminal type 집합은 관측된 initial 실행을 집계하는 실용적인 기준입니다. 모든 노드 terminal event 합계는 “노드 실행 이벤트 수”로 표시하는 편이 정확합니다.

---

**문제 →** 제안한 dedup key는 안정적인 원본 event ID가 아닙니다.

**실패 시나리오 →** query_id 재사용과 동일 microsecond 충돌이 겹치면 서로 다른 이벤트가 합쳐집니다. node alias가 바뀌면 같은 이벤트가 두 개가 됩니다. ReplacingMergeTree의 sorting key에 `collected_at`이나 window 경계를 넣으면 재수집 중복이 제거되지 않습니다.

**수정안 →** 노드 ID를 먼저 안정화하고, key에 query 시작 시각의 microsecond 값처럼 원본 실행을 구별하는 필드를 추가하는 것을 검토하십시오. 이것도 절대적 유일성을 주지는 않으므로 한계를 문서화해야 합니다.

ReplacingMergeTree의 `ORDER BY`는 dedup identity와 일치시키고, 수집 시각·window 정보는 제외합니다. partition 경계를 넘는 중복이 생기지 않게 원본 이벤트 날짜 기준으로 partition하십시오. `FINAL` 또는 명시적 dedup은 **집계 전에** 적용해야 합니다.

---

**문제 →** 현재 p95와 실패율은 관측 조건을 설명하지 않으면 오해를 만듭니다.

**실패 시나리오 →** 성공한 QueryFinish만의 p95는 실패한 장기 쿼리를 제외합니다. query_log 비활성화, sampling, 최소 실행 시간 설정, 노드 누락이 있으면 실제 전체 쿼리 수와 다릅니다.

**수정안 →** p95를 **“성공 쿼리 지연 p95”**로 명시하고 표본 수를 함께 보여주십시오. 실패율 분모는 initial terminal event 전체로 고정합니다. 시간대별 집계는 완료 시각 기준인지 시작 시각 기준인지 정의하고, UTC 저장과 표시 타임존을 분리합니다.

노드별 p95의 평균으로 전체 p95를 계산해서는 안 됩니다. 선택한 노드들의 dedup된 원본 duration을 합쳐 계산해야 합니다.

수집기 제외용 `log_comment`는 원격 query_log에도 실제 전달되는지 확인해야 합니다. 로컬 쿼리 설정이 원격 실행에 자동 전파된다고 가정하지 마십시오.

### 6. Q5 — 줄일 것과 반드시 남길 것

**문제 →** 초안은 RMV 내부에 증분 상태 관리까지 넣어 backend 코드를 줄이려 하지만, 상태 전이와 복구는 오히려 관찰하기 어려워집니다.

**실패 시나리오 →** 장애 후 “어디까지 읽었고 무엇이 저장되었는가”를 SQL 결과와 RMV 예외에서 추론해야 합니다. 데이터 오류와 scheduler 오류가 한 덩어리가 됩니다.

**수정안 →** 저는 PoC를 다음처럼 줄이겠습니다.

- metrics: 노드별 RMV, 15초 snapshot.
- query_log: 노드별 Python 루프, 명시적 cursor, 재조회와 dedup.
- topology: 수동 노드 목록 우선, 자동 발견은 편의 기능.
- 상태: SQLite의 최신 상태와 간단한 전이 이력.
- UI: 현재 제안한 vanilla JS와 SVG 유지.

특히 `collection_status`를 매 폴링마다 append하는 것은 초기에는 이득이 작습니다. 상태가 바뀔 때만 이력을 남기면 충분합니다.

원안의 **“query_log도 반드시 RMV”가 고정 요구라면**, 고정 최근 구간 재조회와 명시적 backfill로 단순화하고 정확성 범위를 좁히는 편을 권합니다.

---

**문제 →** 수집 성공과 데이터 신선도를 같은 상태로 표현하고 있습니다.

**실패 시나리오 →** query_log가 비활성화되었거나 사용량이 없어서 0행이 반환되어도 refresh는 성공합니다. 반대로 metrics가 정상이어도 query_log 권한이 없을 수 있습니다.

**수정안 →** 최소한 다음 세 가지를 분리하십시오.

- 최근 수집 시도 결과와 마지막 성공 시각.
- 성공적으로 검사한 구간의 끝.
- 마지막으로 관측한 이벤트 시각.

0행 성공을 오래된 데이터로 오판하면 안 됩니다. 클러스터 목록의 단일 상태는 이 정보의 요약이어야 하고, 상세에서는 노드·소스별 원인을 볼 수 있어야 합니다.

---

**문제 →** 보존·용량 제한이 없으면 APPEND와 반복 재조회가 로컬 디스크 장애로 이어집니다.

**실패 시나리오 →** 24시간 재조회 중복, 원문 SQL, metrics snapshot이 쌓여 디스크가 찹니다. chDB 쓰기뿐 아니라 SQLite 상태 저장과 복구까지 실패합니다.

**수정안 →** PoC에도 보존 기간, 원격 조회 chunk 크기, 동시 수집 수, 디스크 부족 시 중단 동작을 넣으십시오. `system.metrics`는 시점별 gauge라는 점도 UI에 반영해야 합니다. 이 source만으로 CPU 사용률이나 초당 처리량 같은 모든 운영 지표를 얻을 수는 없습니다.

마지막 검증은 기능 수보다 **장애 후 불변식**에 집중하는 것이 좋습니다. 부분 수신 후 연결 종료, 저장 직후 cursor 갱신 전 강제 종료, overlap보다 늦은 로그 도착, 24시간 초과 중단, 키 오류, 한 노드만 불통인 A 클러스터를 시험하십시오.

제가 승인 기준으로 삼을 문장은 하나입니다. **“수집이 불완전하면 그 구간을 다시 읽거나, 누락 구간을 사용자에게 명시한다.”** 현재 watermark 설계는 이 기준을 충족한다고 보기 어렵고, 명시적 완료 cursor가 그 결함을 가장 작게 고칩니다.


### Round 2

### 전문가 A — Claude/runtime-default/runtime-default
# 라운드 2 — 전문가 A (Claude) 교차 리뷰

## 요약: 합의된 점, 수정한 점, 남은 이견

**C와 합의된 점**

- 진행 상태(cursor)는 데이터와 분리하고, "저장 먼저, cursor 나중" 순서로 커밋합니다.
- query_log는 Python 루프로 수집하고, metrics는 RMV로 유지합니다.
- `system.clusters`는 후보 목록으로만 취급하고, 커버리지 분모는 사용자가 확정한 노드 목록입니다.
- Host 헤더를 검증하고, 쿼리 원문은 비밀 데이터로 취급합니다.
- 키가 없거나 틀려도 자동 재생성하지 않습니다.

**C의 의견을 반영해 수정한 점 (아래 상세)**

- (R1-C의 Q1 "backfill 절단") 24h 초과 다운타임은 **gap 기록**으로 처리합니다.
- (R1-C의 Q4 "partition") ReplacingMergeTree의 **partition 키는 원본 이벤트 시각**으로 고정합니다.
- (R1-C의 Q1 "safety_lag는 완전성 조건이 아님") overlap은 **지연 도착을 측정해서** 결정합니다.

**제 R1 안의 결함 (스스로 철회 또는 정정)**

- RMV sentinel(UNION ALL) 방식은 철회합니다.
- LIMIT 청크 cursor 규칙에 경계 버그가 있어 정정합니다.

**C와 남은 이견**

- double-submit CSRF는 rebinding 상황에서 무력하므로 제거해야 합니다.
- 원격 결과를 "Python이 스트리밍해서 저장"하는 방식은 불필요합니다.

---

## 1. Q1 보강과 정정

### 1-A. 철회: RMV sentinel 행 (R1 1-1)
제가 제안한 `UNION ALL` sentinel은 분기 간 삽입 순서와 실패 원자성에 의존합니다. 이는 C가 R1에서 ORDER BY prefix 가정을 반박한 논리("정렬과 실패 원자성은 다른 성질")와 정확히 같은 종류의 비보장 가정입니다.

따라서 **RMV로 query_log를 수집하는 경로는 C의 대안(고정된 최근 구간 재조회 + dedup + 명시적 backfill)만 허용**하는 것으로 입장을 좁힙니다. 그 외 RMV 변형은 Day 0 게이트를 통과하더라도 권하지 않습니다.

### 1-B. C의 수집 절차 보완: Python이 행을 중계하지 않습니다
C는 "원격 조회 결과를 스트리밍하며 로컬에 저장"한다고 썼습니다. 만약 Python이 행을 받아서 다시 INSERT하는 구조라면 비용이 생깁니다. 직렬화 비용이 들고, 두 번째 쓰기 경로가 생기며, 예외 객체에 행 데이터가 실릴 위험도 있습니다.

권하는 방식은 chDB 안에서 **단일 문장 `INSERT INTO raw SELECT … FROM remoteSecure(nc_x, …) WHERE … {lo} {hi}`**를 실행하는 것입니다. Python은 경계값을 바인딩하고 성공 여부만 판정합니다. "원격 EOF 확인"은 문장이 예외 없이 반환되었는지로 대체됩니다.

### 1-C. 정정: LIMIT 청크 cursor 규칙 (R1 1-5의 버그)
R1에서는 "청크의 마지막 event_time을 hi로 삼는다"고 했는데, 이 규칙에는 두 가지 결함이 있습니다.

- **경계 동률 손실**: 같은 microsecond의 행이 LIMIT 경계에서 잘리면, 다음 청크가 `> last`로 시작하면서 잘린 행이 영구 누락됩니다.
- **무한 루프**: 청크 전체가 같은 타임스탬프이면 cursor가 전진하지 않습니다.

정정한 규칙은 다음과 같습니다.

| 조건 | 다음 cursor |
|---|---|
| 반환 행 수 < LIMIT | 요청한 `hi` 그대로 |
| 반환 행 수 = LIMIT | `last_ts` (**포함 경계**, 다음 청크는 `>= last_ts`로 시작하고 중복은 dedup이 처리) |
| = LIMIT 이고 `first_ts == last_ts` | 해당 ts 하나만 LIMIT 없이 1회 수집한 뒤 `last_ts + 1µs` |

또한 원격 `ORDER BY … LIMIT`가 원격에서 실행되는지는 1-3과 같은 방법으로 실측해야 합니다. 원격에서 실행되지 않으면 청크가 메모리 절감 효과를 내지 못합니다.

### 1-D. 수용: 구간 표기와 backfill gap (R1-C)
- 구간은 `[lo, hi)` 반열림 구간으로 통일하고, 비교는 `DateTime64(6)` 하나로 통일합니다. R1에서 제가 쓴 `> lo AND <= hi` 혼용은 철회합니다.
- 다운타임이 24h를 넘으면 `gaps(node, from, to, reason)` 행을 기록하고, 차트에서 해당 구간을 "미수집"으로 음영 처리합니다. R1에서 저는 "의미가 바뀐다"고 지적만 하고 수정안을 내지 않았는데, C의 안이 맞습니다. **"쿼리 0건"과 "수집 못 함"을 구분**하는 것이 이 PoC의 신뢰성 핵심입니다.

### 1-E. 보강: overlap을 측정 가능한 값으로 만들기
C가 지적했듯 15s/60s는 근거 없는 상수입니다. 여기에 측정 장치를 추가합니다.

- **감사(audit) 재조회**: 10분마다 `[committed_hi - 15m, committed_hi - overlap)` 구간을 재조회합니다. INSERT 전후로 해당 노드·구간의 `count() FINAL` 차이를 기록하면 "overlap 밖에서 늦게 도착한 행 수"가 됩니다.
- 이 값이 0보다 크면 진단 페이지에 경고를 띄우고 overlap을 자동으로 두 배 늘립니다(상한 15m).

이렇게 하면 "보장 범위"를 문서상의 주장이 아니라 관측값으로 제시할 수 있습니다.

### 1-F. 새 지점: chDB 영속성과 cursor 사이의 역전
C가 남긴 전제("chDB 쓰기 완료·영속화 의미 확인")를 구체화하면 다음과 같습니다.

- **문제**: MergeTree는 기본값(`fsync_after_insert=0`)에서 INSERT가 반환되어도 디스크 fsync를 보장하지 않습니다. 반면 SQLite commit은 fsync합니다.
- **실패 시나리오**: 노트북 전원이 갑자기 꺼집니다. 새 part는 유실되었는데 cursor는 이미 전진해 있어서 **조용한 영구 누락**이 생깁니다.
- **수정안 (설정 변경 없이 저비용)**: 기동 시 노드별로 `lo = min(committed_hi, local_max_event_ts) - overlap`으로 계산합니다. 즉 데이터를 진행 상태로 쓰지는 않되, **기동 시 하한 클램프로만** 사용합니다. 정상 상황에서는 아무 영향이 없습니다.

---

## 2. Q4 보강: dedup과 partition (C의 지적 수용·확장)

- **partition**: ReplacingMergeTree의 dedup과 `FINAL`은 **partition 내부에서만** 동작합니다. `PARTITION BY toYYYYMMDD(collected_at)`로 두면 자정을 넘는 overlap 재수집 중복이 영원히 남습니다.
  - 수정안: `PARTITION BY toYYYYMMDD(toDateTime(event_time_microseconds))`로 둡니다. 같은 이벤트는 항상 같은 partition에 들어갑니다.
  - TTL(R1 3-8)도 같은 시각 기준으로 맞춥니다.
- **dedup key**: C가 제안한 `query_start_time_microseconds` 추가에 동의합니다. 비용 없이 query_id 재사용과 동일 µs 충돌 위험을 한 단계 더 줄입니다.
- **C의 우려에 대한 반론**: C의 "node alias가 바뀌면 중복" 문제는 key 확장이 아니라 **nid 정체성**으로 해결해야 합니다. nid는 서버가 생성하고 probe의 `serverUUID()`에 1:1로 바인딩합니다(R1 D-1). 주소 편집이나 alias 변경이 있어도 nid가 유지되므로 key는 안정적입니다. C도 "hostName()만으로 유일성을 가정하지 말라"고 했으니, serverUUID가 그 빈칸을 채웁니다.
- **명칭**: C의 "기록된 initial 쿼리 수" 명칭 제안에 동의합니다. 제가 R1 4-3에서 "재시도를 별개로 센다"고 정의만 명시한 것보다, 라벨 자체를 바꾸는 쪽이 오해를 더 잘 막습니다.

---

## 3. Q5 이견: CSRF 토큰은 빼야 합니다 (C 반박)

C는 "상태 변경은 JSON과 CSRF 검증, SameSite cookie"를 유지하자고 했습니다. 제 반증은 다음과 같습니다.

1. **cross-site 공격은 이미 막혀 있습니다**: `Content-Type: application/json`을 강제하면 cross-origin POST는 CORS preflight를 거치고, 서버가 CORS를 열지 않으면 **요청 자체가 전송되지 않습니다**. Origin 검사는 그 위의 이중 장치입니다.
2. **DNS rebinding 상황에서는 double-submit이 무력합니다**: rebinding이 되면 공격 페이지는 `evil.com:PORT`라는 same-origin이 됩니다. 이때 cookie와 header 값을 **둘 다 공격자가 설정할 수 있으므로** double-submit의 비교는 항상 통과합니다. 이 상황을 막는 것은 Host 검증뿐입니다.

따라서 CSRF 토큰은 방어에 기여하지 않으면서 구현 비용(cookie 발급, JS 주입, 테스트)만 늘립니다. 최종 권고는 **Host allowlist(421 반환) + Origin 검사 + JSON 강제 + CORS 미설정**, 이 네 가지입니다.

---

## 4. C 의견의 기타 보완점

- **상태 3분리 (C의 Q5)**: 동의합니다. "수집 중/지연" 판정의 기준은 **마지막 관측 이벤트가 아니라 `checked_until`(committed_hi)**이어야 합니다. 그래야 유휴 노드가 "지연"으로 오판되지 않습니다. R1에서 제가 제안한 진단 페이지가 이 세 값을 노출하는 곳입니다.
- **"상태 변경 시에만 이력 기록" (C)**: R1에서 저는 이력 테이블 자체를 빼자고 했습니다. Python 경로로 가면 SQLite에 cursor 테이블이 생기므로, 같은 SQLite에 전이 이력을 한 줄씩 남기는 비용은 거의 없습니다. **C의 안으로 수렴**하되, 저장소는 chDB가 아니라 SQLite로 합니다. 키 장애로 chDB가 열리지 않아도 상태 이력은 읽을 수 있어야 하기 때문입니다.
- **하이브리드 운영의 비용 (C와 제 R1 공통의 약점)**: metrics는 RMV, query_log는 Python으로 가면 스케줄러가 두 개, 상태 출처가 두 개(`view_refreshes`와 SQLite)가 됩니다. 실패 모드도 두 벌입니다(RMV의 519/pending_create, NC 참조 metadata의 로드 실패).
  - 수정안: Day 0 스파이크에서 "NC 없이 RMV metadata가 로드되는가"가 실패로 나오면, **metrics도 Python 루프로 통일**합니다. 이 경우 키 장애 복구는 "NC 격리"만으로 끝나고 RMV DROP 절차가 사라집니다.
  - 원안 요구(RMV) 충족은 metrics RMV 하나로 하고, 그것이 운영 복잡도를 키우는 순간 포기하는 기준을 미리 정해 둡니다.
- **원격 시계 (R1 1-2, C가 다루지 않음)**: C의 `[committed_hi - overlap, hi)`에서도 `hi`가 로컬 시계이면 원격이 늦은 skew 상황에서 누락이 생깁니다. `hi`는 원격 `now()` 기준으로 잡는다는 R1 입장을 유지합니다.

---

## 5. 수정된 Day 0 게이트 (R1 대비 추가 항목)

| # | 검증 | 실패 시 결정 |
|---|---|---|
| 1 | NC 참조 `remoteSecure` + 상수 WHERE·ORDER BY·LIMIT가 원격에서 실행되는지 (원격 query_log의 `query`와 `read_rows`로 확인) | 청크 없이 짧은 window로 수집 |
| 2 | `log_comment`가 원격으로 전파되는지 | 원격 `initial_query_id` 접두어 등 대체 표식 사용 |
| 3 | NC가 없을 때 RMV metadata 로드 여부 | metrics도 Python으로 통일 |
| 4 | password round-trip (`'`, `\`, 유니코드) + data dir·로그 grep 0건 | serializer 수정 |
| 5 | **(신규)** 원격 연결을 중간에 끊었을 때 INSERT…SELECT가 부분 커밋하는지 | 부분 커밋 여부와 무관하게 cursor 규칙이 같으므로 **결과 기록만 함** (설계가 이 동작에 의존하지 않음을 확인) |
| 6 | **(신규)** 자정을 넘는 overlap에서 `FINAL` 카운트가 정확한지 | partition 키 재확인 |

최종 승인 기준은 C의 문장을 그대로 채택합니다. **"수집이 불완전하면 그 구간을 다시 읽거나, 누락 구간을 사용자에게 명시한다."** 여기에 하나를 덧붙입니다. **"그 판단은 데이터가 아니라 별도 cursor와 gap 기록에서 나오며, 데이터는 기동 시 하한 클램프로만 쓴다."**

### 전문가 C — Codex/runtime-default/runtime-default
제 입장은 **라운드 1의 C 의견을 유지하되, Python 수집 루프의 청크 처리와 완료 cursor 조건을 더 엄격하게 수정**하는 것입니다. A의 명시적 진행 기록, probe 프로세스 격리, 기동 reconcile 제안에는 동의합니다. 다만 sentinel, timestamp 기반 LIMIT, snapshot 행 수 추정은 새로운 정확성 문제를 만들 수 있어 그대로 채택하면 안 됩니다.

판단 근거는 제공된 실측과 설계입니다. 아직 확인하지 않은 엔진 동작을 확정 사실로 취급하지 않겠습니다.

### 1. Q1 — 완료 cursor는 필요하지만, LIMIT과 결합하면 다시 누락됩니다

**문제 →** A의 1-5는 `ORDER BY event_time_microseconds LIMIT 100000`으로 읽고 마지막 행의 시각까지 진행시키도록 제안합니다. 동일 timestamp를 가진 행이 청크 경계에 걸리면 안전하지 않습니다.

**실패 시나리오 →** 시각 `T`의 행 일부만 LIMIT 안에 들어옵니다. 다음 조회를 `event_time_microseconds > T`로 시작하면 나머지가 누락됩니다. overlap을 유지하더라도 같은 앞부분이 반복해서 LIMIT을 채우면 뒤쪽 행까지 도달하지 못할 수 있습니다. 청크가 끝났다는 사실은 해당 시간 구간 전체를 읽었다는 증거가 아닙니다.

**수정안 →** PoC에서는 **시간 구간을 청크로 나누고, 각 구간은 LIMIT 없이 끝까지 읽은 뒤에만 `committed_hi`를 전진**시키는 편이 단순합니다.

- 기본 구간은 짧게 잡고, 처리량에 따라 다음 구간 폭을 조절합니다.
- 실행 시간·메모리 제한에 걸리면 그 구간을 완료 처리하지 않고 더 작게 나눠 재시도합니다.
- 행 단위 pagination이 꼭 필요하면 안정적인 복합 정렬 키와 동률 처리 규칙을 별도로 설계합니다.

이는 **라운드 1 A의 1-5에 영향받아 제 라운드 1 수집 루프를 보강**한 부분입니다. “명시적 cursor를 사용한다”만으로는 부족하고, cursor가 가리키는 구간을 완전히 읽었다는 조건이 필요합니다.

### 2. sentinel은 원자성이 확인되기 전까지 완료 증거가 아닙니다

**문제 →** A의 1-1에서 제시한 `UNION ALL` progress sentinel은 데이터와 진행 기록을 한 쿼리에 넣지만, 이것이 한 트랜잭션이라는 뜻은 아닙니다.

**실패 시나리오 →** sentinel이 먼저 저장되고 원격 분기가 실패하면 미완료 구간이 완료로 표시됩니다. 반대로 전체 refresh가 원자적으로 반영된다면 가능한 방식이지만, 제공된 실측에는 그 보장이 없습니다.

**수정안 →** sentinel 방식은 기본 설계에서 제외하십시오. 채택하려면 해당 버전의 문서·구현상 보장과 장애 주입 결과가 필요합니다. 성공 사례 몇 번만으로 실패 원자성을 입증할 수는 없습니다.

Python 경로에서는 다음 불변식을 유지합니다.

> 원격 조회 EOF 확인 → 로컬 데이터 쓰기 성공·영속성 조건 충족 → SQLite 완료 cursor 커밋.

제 라운드 1의 “저장 먼저, cursor 나중이면 중복으로 수렴한다”도 **전원 장애 등 고려하는 장애 모델에서 chDB의 완료 응답이 어떤 영속성을 보장하는지**를 전제로 합니다. 이 조건은 명시적으로 남겨야 합니다.

### 3. 원격 시계 기준 hi는 개선이지만, 지연 도착 해결책은 아닙니다

**문제 →** A의 1-2처럼 원격 `now()`로 상한을 정하면 로컬·원격 시계 차이를 줄일 수 있지만, 로그 flush 지연이나 원격 시계 변경은 해결되지 않습니다.

**실패 시나리오 →** 정상적으로 구간을 읽은 뒤, 그 구간에 속하는 query_log 행이 overlap보다 늦게 나타납니다. cursor와 원격 시계가 모두 올바르더라도 누락됩니다.

**수정안 →** 보장 범위를 분리하십시오.

- 완료 cursor: 해당 시점에 조회 가능한 구간을 성공적으로 검사했다는 기록.
- overlap: 일정 범위의 지연 도착을 복구하는 장치.
- 긴 범위 재조회: overlap 밖 지연을 보완하는 선택적 작업.
- gap 기록: backfill 상한이나 원격 보존 기간 때문에 복구하지 못한 구간.

원격 현재 시각 조회는 유용하지만 별도 왕복과 장애 지점도 추가합니다. 시계 skew 측정은 진단으로 유지하고, **어떤 시계 방식을 쓰든 완전성 주장을 overlap·보존 기간의 한계 안으로 제한**해야 합니다.

### 4. Q2·Q3 — 입력 검증과 키 복구는 더 보수적으로 다듬어야 합니다

**문제 →** A의 host 정규식은 ClickHouse의 주소 표현 문법까지 차단하는 검증이라고 보기 어렵습니다. 사용자명 제한도 정상 계정을 불필요하게 배제할 수 있습니다.

**실패 시나리오 →** 점·하이픈만 허용한 host라도 주소 범위처럼 해석되는 표현이 통과할 수 있습니다. 문자열 escaping이 정확해도 의도하지 않은 여러 endpoint 접속이 발생할 수 있습니다.

**수정안 →** **SQL 문자열 안전성과 endpoint 의미 검증을 분리**하십시오. host는 단일 DNS 이름 또는 IP 주소로 파싱하고, port는 별도 정수로 받습니다. ClickHouse의 다중 주소·패턴 표현은 허용하지 않습니다. user/password는 중앙 문자열 serializer로 처리하고 제품상 필요한 제한만 둡니다.

A의 password round-trip 검증에는 동의하지만, 파일 검색에서 비밀이 발견되면 저장 위치부터 구분해야 합니다. NC가 요구대로 암호화되어야 하므로 **암호화된 NC에는 존재할 수 있고, 평문 로그·SQL metadata·응답에는 없어야 한다**는 기준이 정확합니다. 단순 문자열 검색 0건은 암호화의 충분한 검증도 아닙니다.

---

**문제 →** 키 오류 직후 NC 디렉터리를 자동 격리하고 새 키를 만드는 것은 잘못된 키 경로나 일시적 파일 접근 오류를 실제 키 유실로 오인할 수 있습니다.

**실패 시나리오 →** 기존 키를 복구할 수 있는데도 앱이 새 암호화 상태를 만들어 운영자가 복구해야 할 상태가 두 개로 늘어납니다.

**수정안 →** 제 라운드 1의 복구 모드 입장을 유지합니다. 키 오류에서는 엔진을 열지 않고 원본을 보존하며, **키 복원과 자격증명 초기화를 명시적으로 선택**하게 해야 합니다. 격리와 새 키 생성은 초기화를 선택한 뒤 수행합니다. fingerprint는 진단 보조값이며 키 백업을 대체하지 않습니다.

### 5. metrics와 상태에 대한 A의 두 제안은 수정이 필요합니다

**문제 →** A의 3-7은 metric 행 수의 최빈값으로 완전한 snapshot을 추정합니다.

**실패 시나리오 →** 버전 변경으로 metric 종류가 달라지거나, 부분 snapshot이 반복되어 최빈값이 되면 불완전한 데이터를 정상으로 인정합니다.

**수정안 →** snapshot 완전성이 필요하면 `snapshot_id`와 명시적 완료 기록을 사용하십시오. 그렇지 않으면 PoC에서는 “최근 수집값”으로 표시하고 metric별 관측 시각을 보존하는 편이 정직합니다. **행 수 추정치를 정확성 판정으로 사용하지 않겠습니다.**

---

**문제 →** A의 Q5처럼 마지막 성공을 데이터의 최대 시각에서만 복원하면 0행 성공과 부분 삽입을 구별하지 못합니다.

**실패 시나리오 →** query_log는 정상적으로 0행을 반환했는데 성공 시각이 갱신되지 않습니다. 반대로 실패한 metrics refresh가 일부 최신 행을 남기면 성공처럼 보입니다.

**수정안 →** 매 폴링 append 이력은 제거하되, **소스별 마지막 성공한 수집 작업의 기록은 남겨야 합니다.** SQLite에 최신 성공·실패·완료 cursor를 저장하고 상태 전이 때만 이력을 남기면 충분합니다.

또한 연속 2회 실패부터 오류로 표시하는 것은 화면 깜박임을 줄이는 정책입니다. 첫 실패 사실까지 숨기지 말고 마지막 시도 결과는 즉시 노출해야 합니다.

### 6. Q4 — 집계 정확성보다 먼저 관측 범위를 표시해야 합니다

**문제 →** A와 저는 terminal event, initial 구분, dedup 필요성에는 동의하지만, 올바른 집계식만으로 실제 전체 쿼리 수를 얻을 수는 없습니다.

**실패 시나리오 →** 일부 노드가 누락되거나 query_log sampling·최소 실행 시간 설정이 적용되어 있습니다. 화면이 단정적으로 “쿼리 수·실패율·p95”를 표시하면 관측된 표본을 전체 트래픽으로 오해합니다.

**수정안 →** 수치를 **“수집된 initial 실행 기준”**으로 정의하고, 노드 커버리지와 로그 설정 제약을 함께 표시하십시오. 등록 probe에서 확인 가능한 관련 설정을 수집하되, 사용자·쿼리별 override까지 모두 알아냈다고 주장해서는 안 됩니다.

제 라운드 1의 dedup key 보강 제안도 유지합니다. `query_start_time_microseconds` 추가는 충돌 위험을 줄이지만 절대적인 event ID를 만들지는 않습니다. `FINAL`은 설정한 key에 따라 중복을 제거할 뿐, 잘못 정의한 identity를 고쳐주지 않습니다.

### 7. Q5 — 아키텍처 선택은 미확인 가정의 수로 판단하겠습니다

A의 Day 0 스파이크 제안에는 동의합니다. 다만 **26.8 서버만 검증해서는 부족**합니다. 실제 수집 주체인 chDB 4.4.0 / 엔진 26.9.2.1과 서버 26.8의 조합에서 검증해야 합니다.

우선순위는 다음과 같습니다.

1. NC 참조와 비밀 노출 경로.
2. 실패·강제 종료 시 데이터 저장과 cursor의 관계.
3. 시간 조건의 원격 실행과 읽기 비용.
4. 원격 `log_comment` 전파와 수집기 제외.
5. 키 오류 상태에서의 비파괴 복구.

RMV 자체는 metrics 주기 실행이라는 실측된 효용이 있습니다. 반면 query_log에서는 완료 구간 관리·backfill·오류 복구가 이미 필요하므로 Python 루프가 여전히 더 설명 가능하고 검증하기 쉽습니다. 다만 제 라운드 1과 A의 **“스케줄러 약 60줄만 바뀐다”는 표현은 철회할 수준으로 낙관적**입니다. 취소·청크 조절·재시도·삭제와의 경합까지 구현해야 합니다.

최종 권고는 **metrics RMV 유지, query_log는 완전한 시간 구간 단위의 Python 수집, 성공 후 cursor 커밋, 재조회 dedup, 복구 불가 gap 표시**입니다. query_log RMV가 필수 요구라면 고정 최근 구간 재조회와 별도 backfill로 범위를 좁히고, data-max watermark와 prefix 보장 주장은 제거하겠습니다.

