## 결론

**P1 3건, P2 9건을 발견했습니다.** 특히 재시작·수집 설정 변경·서버 시간대 차이에서 **누락된 구간을 수집 완료로 표시할 수 있습니다.**

아래는 코드, 순수 함수 반례 및 기존 파일 확인에 근거한 제 리뷰입니다. 서버를 기동하는 재검증은 하지 않았습니다.

## 발견 사항 — 심각도순

### 1. [P1] 저장된 RMV 설정과 cursor 계산 설정이 재시작 후 달라집니다

**위치:** `mlh/provisioner.py:73-77`, `mlh/schema.py:113-123`, `mlh/status.py:100-116`

- **문제:** reconcile은 RMV의 존재만 확인합니다. 기존 RMV에는 생성 당시 lag/window가 남지만, cursor는 현재 환경변수의 설정으로 계산됩니다.
- **실패 시나리오:** 기존 설정이 `refresh=5, lag=8, overlap=15`이고, 재시작하면서 overlap을 55로 늘립니다. 이전 cursor가 992, 새 성공 시각이 1100이면 실제 RMV는 `[1072,1092)`를 읽지만 backfill은 `[992,1033)`만 생성합니다. **`[1033,1072)`는 읽지 않았는데 완료 구간이 됩니다.** 순수 함수로 이 반례를 확인했습니다.
- **수정:** 노드별 실제 수집 설정/DDL 버전을 영속화하고 cursor도 그 설정을 사용하십시오. 설정 변경은 RMV 변경과 경계 구간 backfill을 포함하는 명시적 migration으로 처리해야 합니다.

### 2. [P1] 장애 복구 backfill이 이전 cursor 직전의 지연 도착 이벤트를 놓칩니다

**위치:** `mlh/status.py:110-116`, `mlh/supervisor.py:104-108`

- **문제:** 복구 구간이 `committed_hi`부터 시작하므로, 정상 수집에서는 overlap으로 재조회했을 이전 구간의 꼬리가 제외됩니다.
- **실패 시나리오:** `lag=10, window=45`, 마지막 성공 시각 1000이면 cursor는 990입니다. 이벤트 시각 985인 행이 flush 지연으로 1005에 나타났는데 앱이 그 사이 중단됩니다. 1100에 재개하면 backfill은 `[990,1046)`이므로 해당 행은 영구 누락됩니다. **지연이 정상 overlap 범위 안이어도 발생합니다.**
- **수정:** 장애 복구 시 이전 cursor에서 지연 도착 허용폭만큼 되감아 재조회하고 FINAL로 중복을 제거하십시오. 성공적으로 조회한 경계와 지연 도착까지 확정한 경계를 구분하는 방법도 가능합니다.

### 3. [P1] `event_date` 필터가 서로 다른 시간대에서 정상 이벤트를 제거합니다

**위치:** `mlh/schema.py:120-122`, `mlh/schema.py:136-137`

- **문제:** source의 `event_date`와 chDB의 기본 시간대로 계산한 날짜를 비교합니다. source의 날짜는 해당 서버의 DateLUT로 생성됩니다. [ClickHouse 구현](https://raw.githubusercontent.com/ClickHouse/ClickHouse/master/src/Interpreters/QueryLog.cpp)
- **실패 시나리오:** chDB가 UTC, source가 America/Los_Angeles인 경우 UTC 10월 27일 00:10의 최근 이벤트는 source에서 `event_date=10월 26일`입니다. RMV의 하한 날짜는 27일이므로 시간 조건에 맞는 이벤트도 제외됩니다. 조회는 성공하므로 cursor는 계속 전진하고 gap도 생기지 않습니다. backfill의 날짜 상한에도 같은 문제가 있습니다.
- **수정:** 우선 날짜 보조 조건을 제거하고 epoch 기반 시간 범위로 정확성을 확보하십시오. 날짜 pruning이 필요하면 source 시간대를 저장하고 그 시간대로 경계를 계산해야 합니다.

### 4. [P2] 테스트 서버가 비밀번호를 preprocessed config에 평문으로 복제합니다

**위치:** `scripts/testenv.py:123`, `scripts/testenv.py:181-198`, `experiments/phase_c_resilience.py:239-246`

- **문제:** `from_env`만으로는 전처리된 설정 파일의 평문 저장을 막지 못합니다.
- **확인된 증거:** 기존 `var/testenv/{a1,a2,b1}/data/preprocessed_configs/users.xml` 모두에서 세 계정의 비밀번호가 검출됐고, `config.xml`에서는 admin 비밀번호가 검출됐습니다. 값 자체는 출력하지 않았습니다. C5는 이 디렉터리를 검사하지 않습니다.
- **리스크:** `secrets.json`을 제외하고 source의 `data/`만 복사해도 비밀번호가 포함됩니다. 다만 이는 **테스트 서버 경로의 문제이며 앱 NC 암호화가 깨졌다는 뜻은 아닙니다.**
- **수정:** 민감 설정에 `hide_in_preprocessed="true"`를 적용하고 기존 사본도 정리하십시오. C5에는 허용된 비밀 파일만 명시적으로 제외한 source 저장 경로를 추가해야 합니다. [공식 설정 문서](https://clickhouse.com/docs/concepts/features/configuration/server-config/configuration-files#encrypting-and-hiding-configuration)

### 5. [P2] 삭제와 supervisor 재시도가 경합하여 RMV가 다시 생성될 수 있습니다

**위치:** `mlh/api.py:192-193`, `mlh/provisioner.py:82-100`, `mlh/supervisor.py:66-70`

- **문제:** HTTP 등록·삭제끼리는 lock을 공유하지만 supervisor의 `retry()`는 그 lock에 참여하지 않습니다. 단일 executor는 **개별 SQL**만 직렬화하지 전체 lifecycle 작업을 원자화하지 않습니다.
- **실패 시나리오:** retrying 노드 삭제 중 두 RMV를 DROP한 직후, supervisor가 아직 남아 있는 registry/NC를 보고 RMV를 재생성합니다. 이후 NC와 registry가 삭제되면 고아 RMV가 남거나 삭제 과정이 실패할 수 있습니다.
- **수정:** 먼저 `deleting` 상태를 영속화하고 retry/backfill에서 제외하십시오. 등록·재시도·삭제 전체를 공통 lifecycle lock 또는 단일 명령 큐로 직렬화해야 합니다.

### 6. [P2] 프로세스 lock보다 먼저 key fingerprint를 변경합니다

**위치:** `mlh/api.py:53-62`, `mlh/chdb_owner.py:85`

- **문제:** registry 초기화, 키 생성 및 fingerprint 기록이 exclusive lock 획득보다 먼저 실행됩니다.
- **실패 시나리오:** 빈 data directory를 서로 다른 `MLH_KEY_FILE`로 두 프로세스가 동시에 초기화하면 둘 다 fingerprint가 없다고 판단할 수 있습니다. 첫 프로세스가 키 A로 chDB를 연 뒤, 두 번째 프로세스가 fingerprint를 B로 덮어쓰고 lock에서 탈락하면 다음 기동부터 정상 키 A가 거부됩니다.
- **수정:** registry/키를 읽거나 변경하기 전에 data directory lock을 획득하고, 초기화 실패 시에도 확실히 해제하십시오.

### 7. [P2] 선택 기간 전체가 미수집이어도 ‘실제 0’으로 표시합니다

**위치:** `mlh/static/cluster.js:72-85`, `mlh/static/cluster.js:136-137`

- **문제:** `anyCoverage()`는 과거 어느 시점에라도 수집 시작 기록이 있었는지만 봅니다. 선택 기간과의 교집합, `committed_hi`, gap을 확인하지 않습니다.
- **실패 시나리오:** 한 시간 전에 중단된 노드에서 최근 15분을 선택하면 해당 기간은 전부 미수집입니다. 그런데 KPI와 빈 테이블은 “실제 0”이라고 표시합니다.
- **수정:** 선택 기간과 노드별 유효 coverage의 교집합을 계산하십시오. 교집합이 없으면 `미수집`, 일부만 있으면 `부분 집계`로 표시하고 집계 대상 구간을 명시해야 합니다.

### 8. [P2] 선택적 topology 검사가 정상 endpoint의 성공 결과까지 없앱니다

**위치:** `mlh/probe.py:53-60`, `mlh/probe.py:146-154`

- **문제:** 자동 발견에서는 `candidates=None`이므로 전체 timeout이 기본 15초입니다. 그러나 자식은 발견한 최대 16개 노드를 순차 검사하고, 모든 검사가 끝나야 결과를 반환합니다.
- **실패 시나리오:** endpoint의 필수 검사는 성공했지만 발견된 여러 노드가 연결 timeout을 일으키면 자식 전체가 종료됩니다. UI에는 endpoint 성공이나 일부 범위 대신 fatal timeout만 전달되어 endpoint만 등록하는 것도 막힙니다.
- **수정:** endpoint 결과를 먼저 보존하고 후보 노드별 deadline을 적용하십시오. 후보 검사 시간 초과는 해당 노드의 미확인/실패로 반환해야 합니다.

### 9. [P2] 수집 권한 오류를 연결 실패로 잘못 표시합니다

**위치:** `mlh/dashboard.py:86`, `mlh/dashboard.py:94-97`, `mlh/static/list.js:16-20`

- **문제:** `last_check.ok`를 단순히 `not errors`로 계산합니다. 수집 오류와 연결·인증 실패를 동일시합니다.
- **실패 시나리오:** 등록 후 `system.query_log` SELECT 권한만 회수하면 metrics 조회와 접속·인증은 계속 성공하지만 목록에는 “연결 실패”가 표시됩니다. 테이블 비활성화나 컬럼 호환성 오류도 동일합니다.
- **수정:** 연결 확인 결과와 수집 결과를 별도로 유지하십시오. 권한·테이블·컬럼 오류로 연결 실패를 단정하지 않아야 합니다.

### 10. [P2] 오류 분류기가 사용자 문자열을 실제 오류 원인으로 해석합니다

**위치:** `mlh/errors.py:29-54`, `mlh/errors.py:58-62`

- **문제:** 전체 exception 문자열을 대상으로 첫 번째 정규식 일치를 선택하므로 사용자명이나 SQL 내용도 분류 근거가 됩니다.
- **실패 시나리오:** 사용자명이 `DNS_ERROR`인 인증 실패 메시지는 명확한 `Code: 516`과 `AUTHENTICATION_FAILED`를 포함해도 `dns`로 분류됩니다. 이 반례를 순수 함수로 확인했습니다.
- **수정:** 실제 exception envelope의 코드·종류를 우선 해석하고 SQL/사용자 데이터 영역을 분류 대상에서 제외하십시오. 모호하면 미분류로 남겨야 합니다.

### 11. [P2] 오래된 PID 파일로 관계없는 프로세스를 종료할 수 있습니다

**위치:** `scripts/testenv.py:230-239`, `scripts/testenv.py:267-279`, `experiments/phase_c_resilience.py:57-67`

- **문제:** PID 존재 여부만 확인하고 해당 프로세스가 이 PoC에서 실행한 서버인지 검증하지 않습니다.
- **실패 시나리오:** 서버 종료 후 남은 PID가 다른 동일 사용자 프로세스에 재사용되면 `stop`이 그 프로세스에 SIGTERM, 경우에 따라 SIGKILL까지 보냅니다.
- **수정:** PID와 함께 시작 시각·실행 파일·config 경로를 검증하십시오. 직접 생성한 자식은 가능한 한 `Popen` 객체로 관리하고, 종료 확인 후 PID 파일을 제거해야 합니다.

### 12. [P2] Phase C의 집합 비교는 중복 집계 결함을 통과시킵니다

**위치:** `experiments/phase_c_resilience.py:109-125`, `experiments/phase_c_resilience.py:155-160`, `experiments/phase_c_resilience.py:258-264`

- **문제:** destination 이벤트를 `set`으로 바꾼 뒤 비교하므로 중복 행이 사라집니다. 수집한 `final_rows`·`initial_queries`에는 기대값 검증이 없고 실패 결과도 종료 코드에 반영하지 않습니다.
- **실패 시나리오:** source 이벤트가 1개인데 destination FINAL에 동일 이벤트가 2개 있어도 `match=true`입니다. 해당 반례를 mock으로 확인했습니다. 양쪽 로그가 비어 있는 경우도 성공할 수 있습니다.
- **수정:** 이벤트 집합 일치뿐 아니라 FINAL 행 수와 initial/실패 건수를 독립 검증하십시오. 발생시킨 query_id의 존재를 확인하고, 필수 조건 실패 시 nonzero로 종료해야 합니다.

## 중대 결함을 찾지 못한 영역

- **실제 Dashboard dedup/중복 집계:** `mlh/schema.py:49-54`의 복합 identity와 FINAL, `mlh/dashboard.py:113-117`의 QueryStart 제외에서 중대 결함은 찾지 못했습니다. 12번은 검증기 문제입니다.
- **SQL injection / identifier 안전성:** parameter binding, NC 리터럴의 역슬래시·따옴표 escaping, 생성 identifier 검증에서 중대 결함은 찾지 못했습니다.
- **Static JS XSS:** 동적 데이터가 `textContent`/text node로 렌더링되며, 검토한 경로에서 실행 가능한 HTML 삽입은 찾지 못했습니다.
- **일반 API 응답의 등록 비밀번호 직접 노출:** 중대 경로는 찾지 못했습니다. 파일 복제 문제는 4번과 별개입니다.

**검증 한계:** 순수 단위 테스트 43개는 통과했습니다. 전체 테스트는 chDB import 중 임시 디렉터리 생성이 read-only 환경에서 차단되어 완료하지 못했으며, 이를 제품 결함으로 판단하지 않았습니다.