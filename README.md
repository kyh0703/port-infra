# port-infra

로컬 개발에 필요한 상태 저장 서비스와 인증 서비스를 Docker Compose로 실행한다.
애플리케이션은 이 Compose에 포함하지 않고 각 저장소의 watch/dev 명령으로 실행한다.
따라서 코드 변경 시 컨테이너 이미지 build/push가 필요 없다.

## 실행

현재 Mac 기준 Docker 런타임은 Kubernetes 없는 Colima를 사용한다.

최초 한 번:

```bash
brew install colima docker docker-compose
colima start --vm-type vz --runtime docker --cpus 4 --memory 6 --disk 60
```

이후 인프라 실행:

```bash
cp .env.example .env
make infra-up
```

Colima 설정은 Docker 전용 4코어/6GB이며 Kubernetes를 설치하지 않는다.

기본 서비스:

| 서비스 | 주소 | 용도 |
| --- | --- | --- |
| PostgreSQL | `localhost:15432` | API, RAG, Keycloak 데이터 |
| Redis | `localhost:6379` | 캐시와 Redis Streams |
| Keycloak | `http://localhost:18080` | `overthinker` realm 인증 |

Keycloak은 기존 PostgreSQL의 `keycloak` 데이터베이스를 그대로 사용한다. 시작 전
`keycloak-db-init`이 전용 계정과 데이터베이스를 보장하고 `.env`의 로컬 비밀번호로
계정을 갱신한다. realm, 사용자, client 데이터는 삭제하지 않는다.

Keycloak issuer:

```text
http://localhost:18080/realms/overthinker
```

API 로컬 환경의 `KEYCLOAK_ISSUER_URL`도 이 주소를 사용한다. Tailnet으로 공개할 때는
`KEYCLOAK_HOSTNAME`을 실제 HTTPS 주소로 바꾸고 Tailscale Serve를 같은 포트로 연결한다.

새 로컬 DB의 개발 계정:

- 애플리케이션 로그인: `admin@overthinker.local` / `admin`
- Keycloak 관리 콘솔: `admin` / `admin`
- API BFF client secret: `overthinker-local-bff-secret`

위 값은 로컬 bootstrap 전용이다. 공유·운영 환경에서는 사용하지 않는다.

## 애플리케이션 실행

앱은 sibling 저장소에서 직접 실행한다. 포트 충돌을 피하기 위한 로컬 기준값:

| 앱 | 포트 |
| --- | --- |
| Web | `3000` |
| Adaptor | `3002` |
| API HTTP | `8000` |
| API gRPC | `8080` |
| RAG HTTP | `8001` |
| Voice Agent health | `8081` |
| Voice Agent metrics | `9091` |
| Aggregator health/metrics | `9101` |

각 앱이 컨테이너 인프라에 접근할 때는 `localhost:15432`와 `localhost:6379`를 사용한다.

## 선택 관리 도구

pgAdmin은 기본 실행에서 제외된다.

```bash
make tools-up
```

- pgAdmin: http://localhost:5050

## 선택 관측 스택

Prometheus와 Grafana는 기본 실행에서 제외된다. 메트릭을 볼 때만 실행한다.

```bash
make observability-up
```

- Prometheus: http://localhost:19090
- Grafana: http://localhost:13000
- 기본 scrape 대상: API `8000`, RAG `8001`, Voice Agent `9091`, Aggregator `9101`, Keycloak

중지:

```bash
make observability-down
```

Loki/Promtail은 포함하지 않는다. native 앱 로그는 각 dev 프로세스의 stdout에서 바로 본다.

## Keycloak realm import와 테마

- 기존 DB가 있으면 DB 상태가 우선이며 별도 import가 필요 없다.
- 새 DB에 realm을 넣을 때 export JSON을 `keycloak/import/`에 둔다.
- realm export에는 사용자 정보가 포함될 수 있어 `keycloak/import/*`는 Git에서 제외된다.
- 현재 Kubernetes에서 사용하던 `overthinker` 로그인 테마는 `keycloak/theme/`에 포함한다.

## 관리 명령

```bash
make infra-logs
make ps
make test
make infra-down
make down
```

기존 PostgreSQL volume에서 `port` 계정 인증이 실패하면:

```bash
make db-ensure-user
```

전체 volume 삭제는 realm과 로컬 데이터를 제거하므로 `docker compose down -v`를 사용하지 않는다.
