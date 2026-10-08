# OpenBao 개인정보 암호화 전환 준비

상태: feature branch 구성·검증 완료, 운영 적용 전.

- API: `scripts/openbao-private-data.py`를 dry-run한 뒤 apply하면 기존 API AppRole 정책에 지정된 data encrypt/decrypt 및 lookup hmac 경로만 추가한다. 기존 PII envelope 권한은 보존한다. 데이터 키는 비반출 ChaCha20-Poly1305, 조회키는 비반출 HMAC 32 bytes다.
- RAG: `scripts/openbao-rag-private-data.py`가 전용 AppRole·키·credential volume을 만든다. API 키에 접근할 수 없다. volume은 UID 1000, 0400 파일이다. 새 credential 쓰기가 실패하면 새 SecretID만 취소한다. 기존 volume은 덮어쓰지 않는다.
- RAG Compose에 전용 credential/CA read-only mount, secrets network, `TMPDIR=/dev/shm`, 1 GiB shared memory를 추가했다. 파서 평문을 디스크에 쓰지 않기 위한 구성이다.
- 두 명령 모두 operator token은 0600 파일에서만 읽고, default는 dry-run이다. 자격 증명·server 응답 body를 출력하지 않는다.

API의 운영 7.x와 main 8.x 계약·DB 차이를 해소하지 않은 채 main feature 이미지를 바로 배포하지 않는다. reader/writer가 함께 전환되고 DB·Redis·기존 backup 등의 평문 정리까지 검증되어야 운영 전환 완료다.

테스트: OpenBao 관련 unittest 43개, Compose static validation 포함, 통과. 운영 OpenBao key/ACL/credential 및 서비스는 변경하지 않았다.
