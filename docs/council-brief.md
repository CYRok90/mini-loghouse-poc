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
