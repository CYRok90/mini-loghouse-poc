# 독립 사용·코드 리뷰 — 2026-10-28

## 1. 결론

**핵심 아이디어의 실행 가능성은 확인했다. 그러나 명세의 정확성·장애 격리 조건까지 모두 충족했다고 보기는 어렵다.**

웹 등록 → 실제 remote/RMV 수집 → 로컬 이력 Dashboard, 암호화 자격증명의 재시작 유지, 작은 workload의 overlap 중복 제거와 backfill은 동작했다. 반면 **P1 2건 + P2 5건**을 코드와 재현으로 확인했다. 특히 수집 구간을 잘못 `full`로 판단하는 문제와, 느린 원격 backfill이 다른 클러스터 조회까지 지연시키는 문제를 우선 해결해야 한다.

- 리뷰 대상: 구현 commit **`f9cd3b0`**. 이후 공개 준비 변경은 README·문서·증거 추가이며 **이 보고서의 앱 결함을 수정한 것이 아니다**.
- 기준: [원래 PoC 명세](../docs/poc-goal.md). 기존 [구현 보고서](feasibility.md)와 구분되는 후속 리뷰다.
- 방법: source 읽기, 기존 단위 테스트, 실제 브라우저, 실제 API, 격리된 ClickHouse 3노드와 별도 backend의 장애·재시작 실험.
- 환경: macOS 27.0.1 arm64, Python 3.11, chDB 4.4.0/core 26.9.0, 내장 engine 26.9.2.1, source ClickHouse 26.8.34.13.
- 안전 경계: 기존 앱 `8765`와 기존 source는 유지했다. 리뷰 소유 앱 `18765` 및 `r1/r2/r3`만 변경했다. 운영 클러스터·업무 데이터는 사용하지 않았다.
- 우선순위는 이 PoC의 관측 신뢰성과 가용성 기준이다. 보안 취약점 등급이나 CVSS가 아니다.

## 2. 근거 — 잘 동작한 부분

| 항목 | 독립 재현 결과 | 증거 |
|---|---|---|
| 기존 단위 테스트 | 55 passed; Starlette/httpx 관련 deprecation warning 1건 | [pytest 결과](review-evidence/unit-tests.txt) |
| 실제 등록·수집 | A TLS 2노드 + B 평문 1노드. 식별한 source 이벤트 A16/B8 = destination FINAL16/8 | [baseline](review-evidence/baseline-results.json) |
| overlap | raw32 / FINAL16 / initial8, 예상 이벤트와 정확히 일치 | [overlap](review-evidence/overlap-results.json) |
| backend 재시작 | 36.01초 중단 중 workload 발생. 재시작 후 등록 ID 유지, A20/B6 총26행 backfill 복구 | [restart](review-evidence/restart-results.json) |
| 이벤트 identity | 위 재시작 실험에 재사용 query_id의 서로 다른 이벤트와 분산 하위 쿼리 포함; 집합과 cardinality 일치 | [restart](review-evidence/restart-results.json) |
| 오류/방어 경로 | 잘못된 password, 권한 부족, 닫힌 포트, TLS↔평문 mismatch, HTTP 응답, 신뢰하지 않는 CA 분류. bad Origin403, bad Host421, 중복409 | [probe matrix](review-evidence/probe-matrix.json) |
| 키 복구 | 키 누락/불일치 → recovery, 연결 미확인·수집 오류. 원키 복원 → normal. 두 번째 owner exit3 | [key recovery](review-evidence/key-recovery-results.json) |
| 자격증명 제한 검사 | 앱 파일1512개에서 알려진 password/key literal 미발견, Dashboard에 password 없음. key0600/data0700 | [검사 범위](review-evidence/credential-check.json) |
| 실제 UI | 빈 목록, 인증 실패, TLS2노드 검사·저장, 첫 수집, 목록·Dashboard, 기간/노드 필터, 오류 쿼리 탭 확인 | [새 캡처](../docs/screenshots/README.md) |

비밀번호 literal 검사에는 압축된 데이터·프로세스 메모리·모든 인코딩이 포함되지 않는다. 따라서 보안 감사 완료나 비밀정보 무유출의 증명으로 확대하지 않는다. HTTP 포트도 실제 응답이 오면 구분되지만, 응답 없이 timeout이 나는 경우 포트 종류를 확정할 수 없다.

### 명세와의 대응

- **핵심 흐름 실증됨:** 등록, 필수 권한 검사, RMV 수집, 실제 차트/테이블, 등록 보존과 제한된 복구 실험.
- **조건부/불충분:** 단일 노드 장애의 다른 조회 경로 격리, 전체 topology 확인, coverage·freshness의 신뢰성, 비동기 화면 일관성.
- **이번 리뷰에서 NOT_RUN:** 실제 Cloud LB/autoscaling, Linux/Docker, 대규모 source·장시간 resource pressure, source 보존기간 만료를 실제로 기다린 실험, 극단적 flush delay/clock skew, 자정 timezone 경계, APPEND 도중 일부 행만 저장되는 실패. 기존 구현 보고서의 실험을 이번 리뷰가 모두 재실행했다고 주장하지 않는다.

## 3. 리스크 — 재현된 주요 문제

### R1 · P1 — 느린 backfill이 다른 클러스터 Dashboard를 지연시킨다

**위치:** [`mlh/chdb_owner.py:88,102–106`](../mlh/chdb_owner.py), [`mlh/supervisor.py:94–114`](../mlh/supervisor.py).

`ChdbOwner`의 단일 foreground executor에서 Dashboard SELECT와 `INSERT ... SELECT remote()` backfill을 함께 실행한다. probe만 subprocess로 격리해도 backfill은 같은 대기열에 남는다. 한 tick에 한 chunk를 처리하는 제한은 chunk 자체가 네트워크를 기다리는 시간을 없애지 못한다.

**실제 재현:** 리뷰 B의 TCP 경로에만 지연을 주고 B에 pending gap을 넣어 backfill을 유도했다. 다른 클러스터 A의 Dashboard 응답은 **0.067초 → 8.226초**, 지연 해제 후 **0.032초**였다. 같은 구간 `/api/health`는0.007초, 목록은0.006초였다. [실측 JSON](review-evidence/slow-link-results.json).

**영향:** 한 source의 느린 원격 작업이 관련 없는 Dashboard까지 기다리게 한다. 모든 HTTP 요청이나 RMV background가 정지했다는 주장은 아니다. 당시 A의 일부 노드 장애 실험도 진행 중이었으므로 “A 전체가 건강했다”는 의미로 해석하지 않는다.

**수정 방향:** 원격 backfill 읽기를 별도 프로세스와 timeout/자원 제한 안에 격리하고, 주 owner에는 bounded local write만 전달한다. provision/reconcile의 원격 schema 조회도 점검해야 한다. 단순 Session/Thread 증설만으로 해결된다고 가정하면 안 된다. [동일 경로 Session 직렬화 실험](../evidence/phase_a2/sessions_params.log)도 있다.

**완료 기준:** B의 connect/receive 지연 중 A의 Dashboard와 상태 갱신이 정한 응답 시간 안에 끝나며, 재시도·취소·종료 시 중복 owner나 유실이 없어야 한다.

### R2 · P1 — flush tail 허용폭으로 내부 gap까지 숨긴다

**위치:** [`mlh/dashboard.py:202–220`](../mlh/dashboard.py), [`mlh/static/cluster.js:90–99,144–147`](../mlh/static/cluster.js).

현재 `full`은 실제 수집 구간의 **연속성**이 아니라 `covered_seconds >= expected_seconds - 1`로 판정한다. tail 허용폭을 총 길이에서 빼므로, tail 밖의 내부/앞쪽 결손도 그 여유로 상쇄된다.

**단위 재현:**

```text
선택 구간     [1000, 1900)
committed_hi 1892
failed gap   [1400, 1409)     # 과거 내부 9초 결손
tail 허용    18초
결과         covered=883, expected=882 → full (기대: partial)
```

앞쪽 gap 일부가 선택 구간과 겹치는 별도 반례도 같은 결과다. [실행 가능한 회귀 기대 테스트](review-evidence/test_review_regressions.py), [실패 결과](review-evidence/regressions.txt).

**영향:** 집계가 비었을 때 UI가 미수집 가능성을 숨기고 `실제 0`으로 안내할 수 있다. 관측 앱의 핵심 의미를 바꾼다.

**수정 방향:** 허용되는 미수집은 **끝부분**으로 제한하고, 필수 구간 `[from, to-tail)` 전체가 수집 span의 합집합에 포함되는지 검사한다. coverage 비율의 분자·분모도 같은 구간으로 clip한다. 여러 gap, gap 경계, 짧은 선택 구간을 회귀 테스트에 포함해야 한다.

### R3 · P2 — “전체 검증됨”이 실제 멤버가 아닌 개수 비교다

**위치:** [`mlh/registration.py:38–46,63–95`](../mlh/registration.py), [`mlh/status.py:133–136`](../mlh/status.py).

discovery는 후보 **개수**만 보존하고, 등록 시 검증한 server UUID가 발견된 멤버 집합을 실제로 채우는지 대응시키지 않는다.

**실제 API 재현:** source `review_a`의 멤버는 **r1+r2**. 명시적 후보로 **r1+r3(다른 클러스터)**를 등록했다. 각 노드 연결·수집은 성공하지만 r2는 수집하지 않는다. 그럼에도 `confirmed_nodes=2`, `candidate_nodes=2`여서 **“전체 검증됨”**을 반환했다. [응답과 source topology](review-evidence/topology-results.json).

**영향:** 정상 UI의 자동 발견만 따른 경우 항상 잘못된다는 뜻은 아니다. 명시적 후보/별칭/다른 구성을 지원하는 API 경계에서 다른 노드가 누락을 대신 충족할 수 있다.

**수정 방향:** discovered member → 검증된 stable server identity 매핑을 보존하고 실제 포함 관계로 판단한다. host 문자열만의 비교는 TLS/plain 별칭을 놓칠 수 있다. 추가 수동 노드는 발견 멤버와 분리하고, 확인하지 못한 범위는 미확인으로 남긴다.

### R4 · P2 — 중단 노드의 metric이 최신 합계처럼 표시된다

**위치:** [`mlh/dashboard.py:173–177`](../mlh/dashboard.py), [`mlh/static/cluster.js:117–127`](../mlh/static/cluster.js).

backend는 최근1일에서 노드별 마지막 값을 반환하고, UI는 모든 값을 합산한 뒤 freshness 시각을 **`max(at_ms)`**로 표시한다. 신선한 한 노드가 오래된 다른 노드의 값을 가린다.

**실제 재현:** r2를 중단해 stale 기준23초를 넘겼다. API에는 r2의 오래된 `MemoryTracking`과 r1의 최신값이 함께 남았다. 이후 UI에서 r2의 약2분14초 전 값까지 합한 **712MiB**가 r1의 최신시각과 함께, metric 자체의 stale 표식 없이 표시됐다. 상단과 노드 표의 수집 오류는 정상 표시되었다. [API snapshot](review-evidence/partial-node-stale.json), [후속 UI 텍스트](review-evidence/partial-node-stale-ui.txt).

**영향:** 사용자에게 현재 클러스터 합계로 읽히는 값이 서로 다른 시점의 합이다. “앱 전체가 정상이라고 표시한다”는 문제와는 구분한다.

**수정 방향:** 합산 전에 노드별 freshness를 검사하고 `fresh/selected` 수, 제외한 stale 노드, 부분 합계를 명시한다. 마지막 관측 합계를 유지한다면 그 의미와 가장 오래된 시각을 표시한다. 시계열의 기여 노드 수와 결손 구간도 반영해야 한다.

### R5 · P2 — 늦게 도착한 이전 Dashboard 응답이 새 필터를 덮어쓴다

**위치:** [`mlh/static/cluster.js:156–175,185–191`](../mlh/static/cluster.js).

`load()`가 요청 당시 필터/세대 번호와 응답 적용을 연결하지 않는다. 필터 변경·자동 갱신·수동 갱신이 겹칠 수 있다.

**실제 브라우저 재현:** API 응답의 내용은 바꾸지 않는 로컬 프록시로 **24h 응답만3초 지연**했다. 24h →15m 순서로 클릭하면 15m 결과가 먼저 보인 뒤 옛24h 결과로 바뀐다. URL·선택 버튼은15m인데 coverage 설명은24h의 `1/1%`가 남았다. [전후 상태](review-evidence/filter-race.json), [실제 화면](review-evidence/filter-race.jpg).

**수정 방향:** 요청마다 filter snapshot과 generation을 잡고 최신 요청만 렌더링한다. polling도 무제한 중첩되지 않게 한다. `AbortController`만으로 server-side 실행 취소까지 보장된다고 가정하지 않는다.

**완료 기준:** 모든 응답 순서 조합에서 표시 데이터가 현재 필터와 일치해야 한다. 노드 변경과 자동 갱신의 교차도 포함한다.

### R6 · P2 — 검사 중 폼을 바꾸면 새 endpoint가 검사 통과로 보인다

**위치:** [`mlh/static/add.js:16–20,45–49,80–92`](../mlh/static/add.js).

입력 변경 시 기존 결과를 invalidate하지만, 진행 중 요청의 응답이 나중에 오면 다시 성공으로 저장한다. 결과 표는 요청 시 값이 아니라 현재 폼의 host/port를 사용한다.

**실제 브라우저 재현:** 정상19123 포트 검사를 시작한 직후19999로 수정했다. 이전 검사 성공이 도착하자 **검사하지 않은 `127.0.0.1:19999`를 필수 검사 통과로 표시하고 저장을 활성화**했다. [화면](review-evidence/probe-input-race.jpg), [UI 텍스트](review-evidence/probe-input-race-ui.txt).

**영향 범위:** backend의 등록 API는 다시 probe하므로 인증/권한 우회가 아니다. 그러나 UI의 검사 결과와 후보 노드가 현재 입력과 불일치한다.

**수정 방향:** 입력 revision과 요청 snapshot을 연결하고 변경된 입력에 대한 이전 결과는 버린다. 또는 검사 동안 관련 입력을 잠근다. 결과의 endpoint는 검사한 snapshot으로 렌더링해야 한다.

### R7 · P2 — 오래된 pending/initial gap은 backfill 한도를 다시 검사하지 않는다

**위치:** [`mlh/supervisor.py:94–122`](../mlh/supervisor.py), [`mlh/status.py:110–125`](../mlh/status.py).

`max_backfill_seconds`는 새 interruption gap을 만들 때 적용되지만 이미 pending인 gap의 실행 직전에는 적용하지 않는다. initial gap 생성도 별도 경로다.

**단위 재현:** 원격 I/O만 fake owner로 대체하고 실제 Registry/Supervisor를 실행했다. `now-2000 .. now-1970`의 initial pending gap, max600초 조건에서 범위 밖 SQL이 실행되고 성공으로 처리된다. [회귀 테스트](review-evidence/test_review_regressions.py), [결과](review-evidence/regressions.txt).

**영향:** 대기열에서 오래 머문 구간에 한도가 무효화된다. source가 이미 해당 로그를 지웠다면 빈 조회 성공을 복구 완료로 오인할 위험이 있다. **실제 TTL 만료를 기다려 데이터 유실까지 재현한 것은 아니다.** 또한 설정한 horizon 자체가 source 보존기간의 증거는 아니다.

**수정 방향:** 각 chunk 실행 전에 현재 floor를 다시 계산하고 오래된 부분은 unrecoverable/unknown으로 남기며 읽을 수 있는 구간만 분리한다. source retention과 로그 존재 여부를 별도로 표현해야 한다.

### 추가 관찰 · P3 — 예외 메시지 안의 가짜 Code 토큰에 분류가 흔들린다

존재하지 않는 테스트 username `Code: 198.`으로 접속하면 실제 인증 실패516의 메시지 안에 사용자명이 들어간다. 분류기가 이를 내부 코드198로 읽어 `dns`를 반환했다. [실제 probe 응답](review-evidence/error-code-in-user.json), [`mlh/errors.py`](../mlh/errors.py).

이전의 단순 `DNS_ERROR` 문자열 반례와 달리 **예외 코드 형태의 사용자 입력**이다. 인증을 통과하거나 데이터를 노출하는 문제는 아니며 진단 오류다. 검증된 예외 구조/경계로 코드를 추출하거나 불확실하면 미분류로 내려야 한다.

## 4. 대안과 수정 순서

1. **R2/R1 우선:** coverage를 interval invariant로 테스트하고, slow source가 다른 조회에 미치는 영향을 제한한다.
2. **UI 진실성 보강:** R4 freshness, R5/R6 latest-request-wins, R3 topology identity를 함께 다룬다.
3. **복구 정책 명시:** R7 horizon 재검사와 source retention/flush delay·clock skew 경계를 분리한다.
4. **그다음 규모 검증:** source 수와 query_log량을 늘리며 foreground latency, RMV queue, 메모리, 디스크/TTL, backfill 처리율을 측정한다.

원안을 폐기할 근거는 아직 없다. 다만 상주 프로세스, 암호화 키, 상태 저장, backfill, 장애 격리 책임은 SQL 기반 수집에서도 사라지지 않는다. 중앙 ClickHouse 서버 또는 별도 수집 계층은 요구 규모에 맞춘 비교 실험의 후보이지, 이번 리뷰에서 검증된 우월한 대안은 아니다.

### 회귀 반례 실행

저장소 루트에서:

```bash
.venv/bin/python -m pytest
# 기존 suite: 55 passed

PYTHONPATH="$PWD" .venv/bin/python -m pytest reports/review-evidence/test_review_regressions.py -q
# 리뷰 대상 구현: 3 failed가 예상 결과 (R2 두 반례 + R7 한 반례)
```

회귀 반례는 올바른 기대 동작을 assert하므로 **현재 구현에서 실패하는 것이 증거**다. 일반 `tests/` suite에 몰래 섞지 않았다. 향후 수정 후에는 이 기대 테스트도 통과하도록 옮겨야 한다.

### 검증 출처의 한계

Council 교차 검토도 시도했으나 한 provider의 응답만 성공하고 다른 provider는 timeout으로 끝났다. 따라서 이 보고서를 **3개 모델 합의 또는 2라운드 완료 결과로 제시하지 않는다**. 판단의 근거는 위 코드 위치와 독립 재현이며, 증거 없는 의견은 확정 사실에 포함하지 않았다.
