"""브라우저로 접근하는 클라이언트 (BFF).

API 경로(/auth/login)가 자격증명을 직접 받는 것과 달리, 여기서는 사용자를
Keycloak 로그인 화면으로 보내고 돌아온 authorization code 만 서버가
토큰으로 바꾼다. 발급된 토큰은 서버 세션에 남고 브라우저에는 세션
식별자만 나가므로, JWT 가 자바스크립트에 노출되지 않는다.

    GET /web           현재 로그인 상태를 보여주는 페이지
    GET /web/login     state + PKCE 를 만들어 Keycloak 으로 리다이렉트
    GET /auth/callback code 를 토큰으로 교환하고 세션 쿠키 발급
    GET /web/logout    세션 정리 후 Keycloak 세션까지 종료
"""

import base64
import hashlib
import html
import json
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings, get_settings
from app.deps import get_keycloak, get_sessions
from app.keycloak import KeycloakClient
from app.session import Session, SessionStore

router = APIRouter(tags=["web"])


def _pkce_pair() -> tuple[str, str]:
    """RFC 7636 의 code_verifier 와 S256 challenge 를 만든다.

    verifier 는 서버 세션에만 두고, 브라우저를 거쳐 Keycloak 에 가는 것은
    해시인 challenge 뿐이다. 그래서 리다이렉트 도중 code 를 가로채도
    verifier 없이는 토큰으로 바꿀 수 없다.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _claims(access_token: str) -> dict:
    """access token 의 페이로드를 읽는다.

    서명 검증은 하지 않는다. 이 토큰은 브라우저를 거치지 않고 서버가
    Keycloak 토큰 엔드포인트에서 직접 받아 온 것이라 출처가 확실하기
    때문이다. 클라이언트가 보내온 토큰을 받는 경우(다음 단계의 보호
    라우트)에는 반드시 JWKS 로 서명을 검증해야 한다.
    """
    payload = access_token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


_STYLE = """
:root { color-scheme: light dark; }
body {
  font-family: system-ui, "Malgun Gothic", sans-serif;
  max-width: 46rem; margin: 4rem auto; padding: 0 1.5rem;
  line-height: 1.7; color: #16202b; background: #f4f6f9;
}
@media (prefers-color-scheme: dark) {
  body { color: #dee5ec; background: #10161d; }
  .card { background: #182029 !important; border-color: #2a3541 !important; }
  th { color: #93a1b0 !important; }
  code { background: #222c37 !important; }
}
h1 { font-size: 1.5rem; margin-bottom: .25rem; }
p.sub { color: #5b6775; margin-top: 0; }
.card { background: #fff; border: 1px solid #d5dee7; border-radius: 6px;
        padding: 1.25rem 1.5rem; margin: 1.5rem 0; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .45rem 0; vertical-align: top; }
th { width: 11rem; font-weight: 500; color: #5b6775; font-size: .9rem; }
code { background: #eaeff5; padding: .1rem .35rem; border-radius: 3px;
       font-size: .88rem; }
a.btn { display: inline-block; background: #1d5b8a; color: #fff;
        padding: .55rem 1.15rem; border-radius: 4px; text-decoration: none; }
a.btn.ghost { background: transparent; color: #1d5b8a;
              border: 1px solid #1d5b8a; }
.note { font-size: .9rem; color: #5b6775; }
"""


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>LLMOps 웹 클라이언트</title>"
        "<style>" + _STYLE + "</style></head><body>" + body + "</body></html>",
        status_code=status_code,
    )


def _row(label: str, value: str) -> str:
    return "<tr><th>" + label + "</th><td>" + value + "</td></tr>"


@router.get("/web", response_class=HTMLResponse)
async def home(
    request: Request,
    settings: Settings = Depends(get_settings),
    sessions: SessionStore = Depends(get_sessions),
):
    session = sessions.get(request.cookies.get(settings.session_cookie_name))

    if session is None:
        return _page(
            "<h1>LLMOps 웹 클라이언트</h1>"
            '<p class="sub">Keycloak 로그인 화면으로 이동해 인증합니다.</p>'
            '<div class="card">'
            "<p>로그인되어 있지 않습니다.</p>"
            '<p><a class="btn" href="/web/login">Keycloak으로 로그인</a></p>'
            "</div>"
            '<p class="note">이 페이지는 authorization code + PKCE 로 로그인하고, '
            "발급받은 토큰은 서버 세션에만 보관합니다. 브라우저에는 세션 "
            "식별자 쿠키만 저장됩니다.</p>"
        )

    claims = session.claims
    roles = claims.get("realm_access", {}).get("roles", [])
    remaining = int(claims.get("exp", 0) - time.time())

    rows = (
        _row("사용자", html.escape(str(claims.get("preferred_username", "-"))))
        + _row("이메일", html.escape(str(claims.get("email", "-"))))
        + _row("realm 역할", html.escape(", ".join(roles) or "-"))
        + _row("발급 클라이언트", "<code>" + html.escape(str(claims.get("azp", "-"))) + "</code>")
        + _row("issuer", "<code>" + html.escape(str(claims.get("iss", "-"))) + "</code>")
        + _row("access token 만료", str(max(remaining, 0)) + "초 남음")
        + _row("토큰 보관 위치", "서버 세션 (브라우저에는 없음)")
    )

    return _page(
        "<h1>로그인됨</h1>"
        '<p class="sub">Keycloak이 발급한 토큰의 클레임입니다.</p>'
        '<div class="card"><table>' + rows + "</table></div>"
        '<p><a class="btn ghost" href="/web/logout">로그아웃</a></p>'
    )


@router.get("/web/login")
async def login(
    settings: Settings = Depends(get_settings),
    sessions: SessionStore = Depends(get_sessions),
):
    verifier, challenge = _pkce_pair()
    # state 는 콜백이 이 브라우저가 시작한 로그인인지 확인하는 CSRF 방어값이다.
    state = sessions.start_login(verifier)

    params = urlencode(
        {
            "client_id": settings.keycloak_web_client_id,
            "response_type": "code",
            "redirect_uri": settings.web_redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return RedirectResponse(settings.authorize_url + "?" + params, status_code=302)


@router.get("/auth/callback")
async def callback(
    request: Request,
    settings: Settings = Depends(get_settings),
    sessions: SessionStore = Depends(get_sessions),
    keycloak: KeycloakClient = Depends(get_keycloak),
):
    params = request.query_params

    if params.get("error"):
        detail = html.escape(params.get("error_description") or params["error"])
        return _page(
            "<h1>로그인 실패</h1>"
            '<div class="card"><p>Keycloak이 오류를 반환했습니다.</p>'
            "<p><code>" + detail + "</code></p></div>"
            '<p><a class="btn ghost" href="/web">처음으로</a></p>',
            status_code=400,
        )

    code, state = params.get("code"), params.get("state")
    attempt = sessions.take_login(state) if state else None
    if not code or attempt is None:
        # state 가 없거나 이미 소비됐다. 콜백 재생이거나 만료된 로그인이다.
        return _page(
            "<h1>로그인 실패</h1>"
            '<div class="card"><p>유효하지 않은 콜백입니다. '
            "state가 만료되었거나 이미 사용되었습니다.</p></div>"
            '<p><a class="btn" href="/web/login">다시 로그인</a></p>',
            status_code=400,
        )

    token = await keycloak.code_grant(code, attempt.code_verifier)
    sid = sessions.create(
        Session(
            access_token=token["access_token"],
            refresh_token=token.get("refresh_token"),
            id_token=token.get("id_token"),
            claims=_claims(token["access_token"]),
        )
    )

    response = RedirectResponse("/web", status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        sid,
        httponly=True,  # 자바스크립트가 읽지 못하게 한다
        samesite="lax",
        secure=False,  # 로컬 http 예제. https 배포에서는 반드시 True
        max_age=settings.session_ttl,
        path="/",
    )
    return response


@router.get("/web/logout")
async def logout(
    request: Request,
    settings: Settings = Depends(get_settings),
    sessions: SessionStore = Depends(get_sessions),
    keycloak: KeycloakClient = Depends(get_keycloak),
):
    session = sessions.pop(request.cookies.get(settings.session_cookie_name))

    # 앱 세션만 지우면 Keycloak 세션이 남아 다시 로그인할 때 화면 없이
    # 통과된다. end_session_endpoint 까지 보내야 실제로 로그아웃된다.
    target = settings.web_post_logout_uri
    if session and session.id_token:
        target = (
            settings.end_session_url
            + "?"
            + urlencode(
                {
                    "id_token_hint": session.id_token,
                    "post_logout_redirect_uri": settings.web_post_logout_uri,
                }
            )
        )

    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response
