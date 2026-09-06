from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, examples=["demo"])
    password: str = Field(min_length=1, examples=["demo1234"])


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """Keycloak 토큰 응답 중 클라이언트가 실제로 쓰는 필드만 노출한다.

    access_token 이 JWT 이고, 이후 단계에서 이 토큰을 검증해 보호 라우트를
    막게 된다. id_token 은 scope 에 openid 가 포함될 때만 내려온다.
    """

    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int
    refresh_expires_in: int | None = None
    scope: str | None = None
