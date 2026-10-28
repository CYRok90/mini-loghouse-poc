# Public 저장 전 점검 — 2026-10-28

## 결론

공개 대상은 이 저장소의 코드·문서·선별한 로컬 테스트 증거다. **runtime data directory, 무작위 테스트 자격증명, 암호화 키, CA/server private key, venv·ClickHouse 바이너리는 포함하지 않는다.** 검사는 공개 위험을 줄이기 위한 제한된 점검이며 전체 보안 감사나 secret-free 보증이 아니다.

## 검사 범위와 결과

- 기존 commit `f9cd3b0`까지 **7개 commit / Git blob163개**를 검사했다. 최신 파일만 검사하고 과거 이력을 무시하지 않았다.
- 프로젝트와 리뷰 fixture의 알려진 runtime secret/key **14개 값**과 JSON escaping·SQL escaping·hex·base64 등을 포함한 **46개 고유 byte 표현**을 대조했다. 기존 이력과 공개할 작업 파일에서 **일치0건**이었다.
- GitHub token, AWS access-key ID, OpenAI project-key, private-key header 패턴 검사에서 **일치0건**이었다.
- 기존 E2E PNG15개, 새 README JPEG4개, 리뷰 반례 JPEG2개를 시각적으로 확인했다. loopback 테스트 endpoint와 테스트 workload이며 password input은 masked/empty다.
- 새 README의 상대 링크와 이미지 경로, JPEG 파일 형식, 일반 테스트 suite와 별도 회귀 반례를 확인했다.
- README 다운로드 SHA256은 ClickHouse의 해당 release asset metadata와 대조했다. 첫 실행 후의 로컬 바이너리와 다운로드 파일은 같다고 가정하지 않았다.
- 기존 실행 중인 앱이 계속 쓰는 `evidence/phase_c/backend-final.log`의 **미커밋 변경은 공개 준비 commit에 넣지 않았다**. 기존에 커밋된 테스트 로그 이력은 유지한다.
- 앱 구현 `mlh/`, `scripts/`, `experiments/`, 기존 `tests/`는 이번 문서·공개 준비에서 변경하지 않았다.

## 예외와 한계

- `experiments/`와 일부 과거 실험 로그에는 **고정 dummy credential/marker 문자열**이 있다. 문법 escaping·저장 위치·누출 경로를 테스트하기 위한 값이며 실제 계정의 비밀번호로 재사용하면 안 된다. 특히 과거 raw 예외 로그의 `leak=False`는 단순 literal 비교였으므로 “어떤 형태로도 password가 없다”는 뜻이 아니다.
- 알 수 없는 secret, 임의 encoding, 압축 파일/메모리, 모든 공급자 token 형식까지 검증하지 않았다. 전용 secret-scanning 서비스의 전체 감사 결과가 아니다.
- query_log 원문에는 실제 환경에서 민감한 SQL과 데이터가 들어갈 수 있다. 새로운 source나 증거를 추가할 때 같은 검사를 다시 해야 한다.
- 저장소 Public과 앱의 네트워크 공개는 별개다. 앱의 loopback 제한·인증/인가 범위는 바꾸지 않았다.

## 참고

- [독립 리뷰](independent-review-2026-10-28.md)
- [실제 캡처 조건](../docs/screenshots/README.md)
- [ClickHouse release](https://github.com/ClickHouse/ClickHouse/releases/tag/v26.8.34.13-lts)
- [해당 버전의 self-extracting executable 구현](https://github.com/ClickHouse/ClickHouse/blob/v26.8.34.13-lts/utils/self-extracting-executable/decompressor.cpp)
