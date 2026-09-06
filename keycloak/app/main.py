"""Keycloak 연계 FastAPI 예제 - 1단계: 로그인 요청과 JWT 토큰 발급.

두 종류의 클라이언트를 함께 보여 준다.

  API   /auth/*  자격증명을 직접 받아 password grant 로 토큰을 발급
  웹    /web/*   브라우저를 Keycloak 로그인 화면으로 보내는 BFF

이 단계에서 앱은 토큰을 "발급받아 보관"하기만 한다. 토큰 검증(JWKS 서명
확인), 보호 라우트, 역할 기반 인가는 다음 단계에서 붙인다.
"""

from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI

from app.config import Settings, get_settings
from app.deps import get_keycloak
from app.keycloak import KeycloakClient
from app.schemas import LoginRequest, LogoutRequest, RefreshRequest, TokenResponse
from app.session import SessionStore
from app.web import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 커넥션 풀을 프로세스당 하나만 둔다. 요청마다 클라이언트를 만들면
    # 인증 경로에서 TCP/TLS 핸드셰이크 비용을 매번 치르게 된다.
    async with httpx.AsyncClient(timeout=settings.keycloak_timeout) as http:
        app.state.settings = settings
        app.state.keycloak = KeycloakClient(settings, http)
        app.state.sessions = SessionStore(settings.session_ttl)
        yield


app = FastAPI(
    title="keycloak-auth-api",
    description="Keycloak에서 JWT를 발급받는 최소 예제 (API 경로 + 웹 경로)",
    lifespan=lifespan,
)
app.include_router(web_router)


@app.get("/health")
async def health(settings: Settings = Depends(get_settings)):
    return {"status": "ok", "realm": settings.keycloak_realm}


@app.get("/health/keycloak")
async def health_keycloak(keycloak: KeycloakClient = Depends(get_keycloak)):
    """realm discovery 문서를 읽어 Keycloak 연결과 realm 존재를 함께 확인한다."""
    document = await keycloak.discovery()
    return {
        "status": "ok",
        "issuer": document["issuer"],
        "token_endpoint": document["token_endpoint"],
        "jwks_uri": document["jwks_uri"],
    }


@app.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    keycloak: KeycloakClient = Depends(get_keycloak),
):
    """사용자 자격증명으로 Keycloak에서 JWT를 발급받는다."""
    token = await keycloak.password_grant(body.username, body.password)
    return TokenResponse(**token)


@app.post("/auth/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    keycloak: KeycloakClient = Depends(get_keycloak),
):
    """refresh token으로 access token을 재발급한다.

    Keycloak 기본 설정은 회전(rotation)이라 응답의 refresh_token 도 새 값이다.
    호출한 쪽은 반드시 새 값으로 교체 저장해야 한다.
    """
    token = await keycloak.refresh_grant(body.refresh_token)
    return TokenResponse(**token)


@app.post("/auth/logout", status_code=204)
async def logout(
    body: LogoutRequest,
    keycloak: KeycloakClient = Depends(get_keycloak),
):
    await keycloak.logout(body.refresh_token)
