# 독립 리뷰 증거

[리뷰 보고서](../independent-review-2026-10-28.md)의 근거로 선별한 실제 실행 결과입니다. 기존 앱과 분리된 로컬 fixture에서 얻었으며 private data directory, 키, secrets 파일, 원본 runtime dump는 포함하지 않았습니다.

- JSON: 실제 API/실행 결과. snapshot별 측정 시각은 다를 수 있습니다.
- JPG/TXT: 실제 브라우저의 화면/표시 내용. race를 재현한 화면은 정상 동작 예시가 아닙니다.
- `test_review_regressions.py`: R2와 R7의 최소 반례. 올바른 기대 동작을 assert하므로 현재 구현에서는 **3개 실패가 예상**됩니다.
- `unit-tests.txt`: 기존 일반 suite 결과.
- `regressions.txt`: 위 최소 반례의 별도 실행 결과.

이 증거는 작은 로컬 PoC의 동작 확인이며 전체 보안 감사·대규모 부하 검증·운영 무손실 보증이 아닙니다. 공개 전 검사의 범위와 한계는 [publication-audit.md](../publication-audit.md)에 기록합니다.
