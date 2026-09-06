"""라우터가 공유하는 의존성.

lifespan 에서 app.state 에 올려 둔 객체를 꺼내 준다. 라우터끼리 서로를
import 하지 않도록 이 모듈을 사이에 둔다.
"""

from fastapi import Request

from app.keycloak import KeycloakClient
from app.session import SessionStore


def get_keycloak(request: Request) -> KeycloakClient:
    return request.app.state.keycloak


def get_sessions(request: Request) -> SessionStore:
    return request.app.state.sessions
