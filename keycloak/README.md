# Keycloak + FastAPI 연계 예제 (1단계: 로그인 / JWT 발급)

Keycloak을 인증 서버로 두고 **JWT를 발급받는 것까지**를 구현한 예제다.
접근 방식이 다른 두 클라이언트가 한 realm을 공유한다.

```
API   클라이언트 --자격증명--> [FastAPI /auth/login] --password grant--> [Keycloak]
                                      <-- access/refresh token --

웹    브라우저 --> [/web/login] --302--> [Keycloak 로그인 화면]
                                              |
      브라우저 <--302 code-- [Keycloak] <--인증--+
                 |
                 +--> [/auth/callback] --code 교환--> [Keycloak]
                          토큰은 서버 세션에, 브라우저에는 세션 쿠키만
```

토큰 검증(JWKS 서명 확인), 보호 라우트, 역할 기반 인가는 다음 단계에서 붙인다.

## 구성

| 파일 | 역할 |
|---|---|
| `docker-compose.yml` | PostgreSQL + Keycloak 26(realm 자동 import) + FastAPI 앱 |
| `realm/llmops-realm.json` | realm `llmops`, 클라이언트 2개, 테스트 사용자 사전 정의 |
| `app/config.py` | 환경변수 설정. 백채널/브라우저용 Keycloak 주소를 분리해 조립 |
| `app/keycloak.py` | Keycloak OIDC 호출(password · refresh · code grant)과 오류 정규화 |
| `app/session.py` | 서버 세션 저장소 (인메모리) |
| `app/web.py` | 브라우저 클라이언트 (BFF) |
| `app/deps.py` | 라우터가 공유하는 의존성 |
| `app/main.py` | API 라우터와 앱 조립 |

포트: Keycloak `8180`, FastAPI `3200`. 네트워크는 다른 llmops 프로젝트와 같은
`llmops-net`(external)을 쓴다. Keycloak의 realm·사용자·클라이언트는 모두
PostgreSQL에 저장되고, 데이터는 명명 볼륨 `keycloak_pgdata`에 남는다.

사전 생성되는 자격정보:

| 항목 | 값 |
|---|---|
| Keycloak 관리자 | `admin` / `admin` |
| realm | `llmops` |
| API 클라이언트 | `llmops-api` — confidential, Direct Access Grants 켬 |
| 웹 클라이언트 | `llmops-web` — confidential, Standard Flow + PKCE(S256) |
| client secret | `llmops-api-secret` / `llmops-web-secret` |
| 테스트 사용자 | `demo` / `demo1234`, `admin-user` / `admin1234` |

## 실행

네트워크가 없다면 먼저 만든다.

```bash
docker network create llmops-net
```

기동:

```bash
docker compose -f llmops/keycloak/docker-compose.yml up -d --build
```

Keycloak이 `healthy`가 될 때까지 30초 안팎 걸린다. 관리 콘솔은
<http://localhost:8180> (admin/admin), API 문서는 <http://localhost:3200/docs>.

## 설정은 어디까지 남는가

Keycloak은 기동할 때마다 import 디렉터리를 읽지만, realm이 이미 있으면
건너뛴다(`Realm 'llmops' already exists. Import skipped`). 그래서 파일을 고쳐도
DB에 이미 realm이 있으면 반영되지 않는다.

| 명령 | DB | 콘솔에서 바꾼 설정 | 사용자 `sub` | realm JSON |
|---|---|---|---|---|
| `restart` | 유지 | 유지 | 그대로 | 건너뜀 |
| `down` + `up` | 유지 | 유지 | 그대로 | 건너뜀 |
| `down -v` + `up` | **삭제** | **소멸** | **새로 생성** | 다시 import |

앱 DB에 `sub`를 저장해 사용자를 매핑한다면 `down -v`는 그 매핑을 전부
끊는다는 뜻이다. realm 파일을 고쳐 반영해야 할 때만 쓰고, 평소에는
`down`까지만 쓴다.

콘솔에서 만진 내용을 파일로 되돌리려면 부분 export를 쓴다. 사용자는 포함되지
않고 client secret은 마스킹되므로 그대로 커밋해도 된다.

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" "http://localhost:8180/admin/realms/llmops/partial-export?exportClients=true&exportGroupsAndRoles=true" -o llmops/keycloak/realm/llmops-realm.json
```

## 웹 클라이언트로 로그인

브라우저로 <http://localhost:3200/web> 을 연다.

1. "Keycloak으로 로그인" → `state`와 PKCE `code_challenge`를 만들어 Keycloak 인가 엔드포인트로 이동
2. Keycloak 로그인 화면에서 `demo` / `demo1234`
3. `/auth/callback`으로 authorization code가 돌아오고, 서버가 이를 토큰으로 교환
4. 사용자명·역할·issuer·만료 시간이 표시된다

발급된 토큰은 서버 세션에만 있고 브라우저에는 `llmops_session` 쿠키(HttpOnly)만
저장된다. "로그아웃"은 앱 세션을 지운 뒤 Keycloak의 `end_session_endpoint`까지
호출해 SSO 세션을 함께 끊는다.

## API 클라이언트로 토큰 발급

연결 확인 — realm의 OIDC discovery 문서를 읽어온다.

```bash
curl -s http://localhost:3200/health/keycloak
```

로그인 및 토큰 발급:

```bash
curl -s -X POST http://localhost:3200/auth/login -H "Content-Type: application/json" -d '{"username":"demo","password":"demo1234"}'
```

응답의 `access_token`이 JWT다. 페이로드를 눈으로 확인하려면:

```bash
curl -s -X POST http://localhost:3200/auth/login -H "Content-Type: application/json" -d '{"username":"demo","password":"demo1234"}' | python -c "import sys,json,base64;t=json.load(sys.stdin)['access_token'].split('.')[1];print(json.dumps(json.loads(base64.urlsafe_b64decode(t+'='*(-len(t)%4))),indent=2,ensure_ascii=False))"
```

잘못된 비밀번호는 401로 정규화된다.

```bash
curl -i -s -X POST http://localhost:3200/auth/login -H "Content-Type: application/json" -d '{"username":"demo","password":"wrong"}'
```

토큰 재발급 / 세션 종료:

```bash
curl -s -X POST http://localhost:3200/auth/refresh -H "Content-Type: application/json" -d '{"refresh_token":"<refresh_token>"}'
```

```bash
curl -i -s -X POST http://localhost:3200/auth/logout -H "Content-Type: application/json" -d '{"refresh_token":"<refresh_token>"}'
```

## 호스트에서 앱만 직접 실행

Keycloak만 컨테이너로 띄우고 앱은 로컬에서 돌릴 수도 있다.

```bash
docker compose -f llmops/keycloak/docker-compose.yml up -d keycloak
```

```bash
cp llmops/keycloak/.env.example llmops/keycloak/.env
```

```bash
cd llmops/keycloak && uv run uvicorn app.main:app --reload --port 3200
```

## 설계 메모

- **왜 클라이언트를 둘로 나눴나.** 두 접근 방식은 필요한 grant가 다르다.
  웹 클라이언트에는 Direct Access Grants를 끄고, API 클라이언트에는 켠다.
  하나의 클라이언트에 둘 다 켜면 웹 전용 클라이언트가 password grant까지
  받아들이게 되어 공격면이 넓어진다.
- **issuer 고정.** `KC_HOSTNAME`을 `http://localhost:8180`으로 박고
  `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`를 켰다. 브라우저는 `localhost:8180`,
  앱은 `keycloak:8080`으로 각각 접근하지만 발급되는 토큰의 `iss`는 항상
  `http://localhost:8180/realms/llmops`로 같다. 이 설정이 없으면 Keycloak이
  요청 Host로 issuer를 만들어 검증 단계에서 어긋난다.
- **PKCE는 confidential 클라이언트에도 켠다.** client secret이 있어도
  authorization code가 리다이렉트 도중 가로채이는 경로는 남는다.
  `code_verifier`는 서버에만 있으므로 code만으로는 토큰을 얻지 못한다.
- **Keycloak 저장소는 PostgreSQL이다.** 내장 H2(dev-file)는 컨테이너의 쓰기
  레이어에만 있어 컨테이너를 지우면 사라지고, 그때 사용자 UUID(`sub`)가 새로
  발급된다. 앱이 `sub`로 사용자를 매핑하는 이상 이 값은 안정적이어야 한다.
  `start-dev` 를 유지한 것은 HTTP 허용과 TLS 설정 생략을 위해서이고, 운영에서는
  `start` 와 TLS·프록시 설정이 추가로 필요하다.
- **세션은 인메모리다.** 앱을 재기동하면 로그인 세션이 모두 끊기고, 앱을
  여러 개로 늘리면 인스턴스마다 세션이 달라진다. 실제 배포에서는 Redis 같은
  공유 저장소로 바꿔야 한다.
- **콜백에서 서명 검증을 하지 않는다.** 토큰을 브라우저가 아니라 서버가
  Keycloak에서 직접 받아 오므로 출처가 확실하다. 반대로 클라이언트가 보내온
  토큰을 받는 다음 단계의 보호 라우트에서는 JWKS 검증이 필수다.
- **client secret이 realm 파일에 들어 있다.** 로컬 예제라 재현성을 위해
  고정값을 넣었다. 실제 환경에서는 import 파일에서 빼고
  `KEYCLOAK_CLIENT_SECRET` / `KEYCLOAK_WEB_CLIENT_SECRET`으로만 주입한다.
- **로그아웃의 범위.** refresh token으로 SSO 세션을 끊을 뿐, 이미 발급된
  access token은 만료(기본 5분) 전까지 서명상 유효하다.

## 다음 단계 (2단계 예정)

JWKS로 access token 서명을 검증하고, `Depends`로 보호 라우트를 만들고,
`realm_access.roles` 기반 역할 인가를 추가한다.
