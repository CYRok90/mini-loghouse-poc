# 실제 UI 캡처 기록

- 날짜: **2026-10-28**, 화면 시간대 `Asia/Seoul`.
- 대상 코드: `f9cd3b0`와 동일한 앱 구현. README용으로 UI·DOM·API 응답을 꾸미거나 합성하지 않았다.
- backend: 기존 앱과 분리된 `http://127.0.0.1:18765`.
- source: ClickHouse 26.8.34.13, 일회용 로컬 인스턴스3개.
  - `demo-analytics (TLS)`: r1 `127.0.0.1:19511` + r2 `127.0.0.1:19512`, CA 검증 TLS.
  - `demo-events (local)`: r3 `127.0.0.1:19113`, 격리된 로컬 평문.
- 계정: fixture가 생성한 일회용 `mlh_collector`. 실제 클러스터 자격증명을 사용하지 않았다.
- 수집 설정: refresh5초, lag8초, overlap15초, initial backfill900초.
- 브라우저: 실제 앱을 조작해 JPEG 캡처. 기본 viewport1280×720을 유지했으며, 긴 화면은 full-page capture의 영역을 선택했다. 이미지 후처리·데이터 조작 없음.

| 파일 | 보여 주는 동작 |
|---|---|
| [register-tls.jpg](register-tls.jpg) | 웹 폼 입력 → 인증·system table 권한·2노드 TLS 검사 성공, 저장 직전 |
| [cluster-list.jpg](cluster-list.jpg) | 두 클러스터가 함께 수집 중인 목록. 빈 여백만 제외 |
| [dashboard.jpg](dashboard.jpg) | 실제 등록된 TLS 클러스터의 상태·노드·KPI·쿼리 수/p95 차트 |
| [metrics-errors.jpg](metrics-errors.jpg) | 같은 Dashboard의 metrics와 오류 쿼리 탭 |

## workload

실제 source에서 `demo-readme-<node>-<sequence>` query_id를 사용했다. 노드마다35회의 성공 쿼리와3회의 의도적 실패 쿼리를 실행했다. Dashboard에는 그 밖의 로컬 검증 쿼리도 포함될 수 있으므로 KPI가 이 workload 수와 정확히 같아야 하는 것은 아니다.

```sql
-- 작은 합계 쿼리에 실제 지연을 더해 p95와 시계열을 관찰
SELECT sum(number), sleep(0.12) FROM numbers(200000);

-- 오류 테이블 확인용, 테스트 source에서만 실행
SELECT throwIf(1, 'Intentional demo failure');
```

쿼리는 약4초 간격으로 발생시켰다. 이후 source log flush·collector lag를 거쳐 RMV가 저장한 결과를 브라우저에서 확인했다. 초기 빈 chart에 값을 삽입하거나 JSON fixture로 대체하지 않았다. 캡처는 실행 결과의 한 시점이며 지속적인 가용성 보장은 아니다.

## 기본 설치와 포트가 다른 이유

README의 일반 데모는 `8765`, `19401/19402/19003`을 쓴다. 캡처는 이미 실행 중인 앱에 영향을 주지 않도록 별도 data directory·키·CA·포트로 수행했다. 재현할 때는 [빠른 시작](../../README.md#빠른-시작)의 기본 포트를 사용해도 된다.

화면의 정상 사례와 별개로 알려진 장애·집계·race 반례는 [독립 리뷰](../../reports/independent-review-2026-10-28.md)에 공개했다.
