"""Keycloak OpenID Connect 엔드포인트 호출부.

FastAPI 라우터는 Keycloak의 응답 형식을 몰라도 되도록, 여기서 HTTP 호출과
오류 변환을 모두 처리한다. 두 클라이언트가 한 realm을 함께 쓴다.

  llmops-api  password grant 로 토큰을 직접 발급받는 API 경로
  llmops-web  브라우저 리다이렉트(Authorization Code + PKCE) 경로

둘 다 confidential 이므로 요청 본문에 client_id/client_secret 을 실어
보낸다(client_secret_post).
"""

import httpx
from fastapi import HTTPException, status

from app.config import Settings


class KeycloakClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    @property
    def _api_credentials(self) -> dict[str, str]:
        return {
            "client_id": self._settings.keycloak_client_id,
            "client_secret": self._settings.keycloak_client_secret,
        }

    @property
    def _web_credentials(self) -> dict[str, str]:
        return {
            "client_id": self._settings.keycloak_web_client_id,
            "client_secret": self._settings.keycloak_web_client_secret,
        }

    async def password_grant(self, username: str, password: str) -> dict:
        """Resource Owner Password Credentials Grant.

        realm 의 클라이언트에서 Direct Access Grants 가 켜져 있어야 한다.
        사용자 자격증명이 백엔드를 통과하므로, 브라우저를 쓸 수 있는 경우에는
        아래 code_grant 쪽이 원칙이다.
        """
        return await self._token_request(
            {
                "grant_type": "password",
                "username": username,
                "password": password,
                # openid 를 넣어야 id_token 이 함께 발급된다.
                "scope": "openid profile email",
                **self._api_credentials,
            }
        )

    async def refresh_grant(self, refresh_token: str) -> dict:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                **self._api_credentials,
            }
        )

    async def code_grant(self, code: str, code_verifier: str) -> dict:
        """Authorization Code Grant + PKCE.

        브라우저가 받아 온 code 를 서버가 토큰으로 바꾼다. redirect_uri 는
        인가 요청 때 보낸 값과 글자 단위로 같아야 하고, code_verifier 는
        인가 요청에 실었던 challenge 의 원본이어야 한다.
        """
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.web_redirect_uri,
                "code_verifier": code_verifier,
                **self._web_credentials,
            }
        )

    async def logout(self, refresh_token: str) -> None:
        await self._logout(refresh_token, self._api_credentials)

    async def web_logout(self, refresh_token: str) -> None:
        await self._logout(refresh_token, self._web_credentials)

    async def discovery(self) -> dict:
        try:
            response = await self._http.get(self._settings.discovery_url)
        except httpx.RequestError as exc:
            raise _unreachable(exc) from exc
        if response.status_code >= 400:
            raise _translate(response)
        return response.json()

    async def _logout(self, refresh_token: str, credentials: dict[str, str]) -> None:
        """refresh token 이 속한 SSO 세션을 종료한다.

        이미 발급된 access token 은 만료 전까지 서명상 유효하다. 즉시 차단이
        필요하면 토큰 검증 단계에서 introspection 을 함께 써야 한다.
        """
        try:
            response = await self._http.post(
                self._settings.logout_url,
                data={"refresh_token": refresh_token, **credentials},
            )
        except httpx.RequestError as exc:
            raise _unreachable(exc) from exc

        # Keycloak 은 성공 시 204, 이미 만료된 토큰에도 관대하게 응답한다.
        if response.status_code >= 400:
            raise _translate(response)

    async def _token_request(self, form: dict[str, str]) -> dict:
        try:
            response = await self._http.post(self._settings.token_url, data=form)
        except httpx.RequestError as exc:
            raise _unreachable(exc) from exc

        if response.status_code >= 400:
            raise _translate(response)
        return response.json()


def _unreachable(exc: httpx.RequestError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"keycloak에 연결할 수 없습니다: {exc.__class__.__name__}",
    )


def _translate(response: httpx.Response) -> HTTPException:
    """Keycloak 오류를 API 오류로 정규화한다.

    Keycloak 은 자격증명 오류를 401 invalid_grant 로, 클라이언트 설정 오류를
    400 unauthorized_client 등으로 돌려준다. 앞의 것만 인증 실패로 넘기고
    나머지는 서버 구성 문제이므로 502 로 구분해야 원인 파악이 빠르다.
    """
    try:
        body = response.json()
    except ValueError:
        body = {}

    error = body.get("error", "")
    description = body.get("error_description") or body.get("errorMessage") or ""

    if error == "invalid_grant":
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=description or "자격증명이 올바르지 않습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"keycloak 오류({response.status_code}) {error}: {description}".strip(),
    )
