"""환경변수 기반 설정.

앱 코드는 os.environ을 직접 읽지 않는다. 로컬 실행(.env), 컨테이너 실행
(docker-compose environment), 테스트가 각각 다른 Keycloak을 바라볼 수 있게
모든 값을 이 클래스 하나로 모은다.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 서버 간 통신에 쓰는 Keycloak 주소. 컨테이너 안에서는 서비스명(keycloak),
    # 호스트에서 직접 실행할 때는 localhost:8180 을 쓴다.
    keycloak_base_url: str = "http://keycloak:8080"

    # 브라우저가 리다이렉트로 찾아가는 주소. 컨테이너 내부 주소로는 브라우저가
    # Keycloak에 닿을 수 없으므로 백채널 주소와 반드시 분리해야 한다.
    keycloak_public_base_url: str = "http://localhost:8180"

    keycloak_realm: str = "llmops"

    # API 클라이언트. password grant 로 토큰을 직접 발급받는 경로에 쓴다.
    keycloak_client_id: str = "llmops-api"
    keycloak_client_secret: str = "llmops-api-secret"

    # 웹 클라이언트. 브라우저 리다이렉트(Authorization Code + PKCE) 경로에 쓴다.
    keycloak_web_client_id: str = "llmops-web"
    keycloak_web_client_secret: str = "llmops-web-secret"

    # Keycloak 이 이 값과 정확히 일치하는 redirect_uri 만 허용한다.
    web_redirect_uri: str = "http://localhost:3200/auth/callback"
    web_post_logout_uri: str = "http://localhost:3200/web"

    # 세션 식별자만 담는 쿠키. 토큰 자체는 브라우저로 나가지 않는다.
    session_cookie_name: str = "llmops_session"
    session_ttl: int = 1800

    # Keycloak 호출 타임아웃(초). 인증은 요청 경로에 직접 걸리므로 짧게 잡는다.
    keycloak_timeout: float = 5.0

    @property
    def realm_url(self) -> str:
        return f"{self.keycloak_base_url.rstrip('/')}/realms/{self.keycloak_realm}"

    @property
    def public_realm_url(self) -> str:
        return f"{self.keycloak_public_base_url.rstrip('/')}/realms/{self.keycloak_realm}"

    @property
    def token_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/token"

    @property
    def logout_url(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/logout"

    @property
    def discovery_url(self) -> str:
        return f"{self.realm_url}/.well-known/openid-configuration"

    # 아래 둘은 브라우저가 직접 여는 주소라 public 주소를 쓴다.
    @property
    def authorize_url(self) -> str:
        return f"{self.public_realm_url}/protocol/openid-connect/auth"

    @property
    def end_session_url(self) -> str:
        return f"{self.public_realm_url}/protocol/openid-connect/logout"


@lru_cache
def get_settings() -> Settings:
    return Settings()
