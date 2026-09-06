"""브라우저 세션 저장소.

BFF 구성이라 토큰은 서버에만 남고 브라우저에는 세션 식별자만 나간다.
프로세스 메모리에 두므로 재기동하면 모든 세션이 끊기고, 앱을 여러 개로
늘리면 붙는 인스턴스마다 세션이 달라진다. 실제 배포에서는 Redis 같은
공유 저장소로 바꿔야 하는 지점이다.
"""

import secrets
import time
from dataclasses import dataclass, field


@dataclass
class LoginAttempt:
    """인가 요청을 보낸 뒤 콜백이 돌아올 때까지만 사는 상태."""

    code_verifier: str
    created: float = field(default_factory=time.time)


@dataclass
class Session:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    claims: dict
    created: float = field(default_factory=time.time)


class SessionStore:
    # 인가 요청부터 콜백까지 걸리는 시간. 사용자가 로그인 화면에 머무는
    # 시간이라 넉넉히 잡되, 무한정 남겨 두면 state 재사용 창구가 된다.
    LOGIN_TTL = 300

    def __init__(self, session_ttl: int) -> None:
        self._logins: dict[str, LoginAttempt] = {}
        self._sessions: dict[str, Session] = {}
        self._session_ttl = session_ttl

    def start_login(self, code_verifier: str) -> str:
        self._sweep()
        state = secrets.token_urlsafe(32)
        self._logins[state] = LoginAttempt(code_verifier=code_verifier)
        return state

    def take_login(self, state: str) -> LoginAttempt | None:
        """state 를 소비한다. 한 번 쓰면 사라지므로 콜백 재생 공격을 막는다."""
        attempt = self._logins.pop(state, None)
        if attempt is None or time.time() - attempt.created > self.LOGIN_TTL:
            return None
        return attempt

    def create(self, session: Session) -> str:
        self._sweep()
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = session
        return sid

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        if time.time() - session.created > self._session_ttl:
            self._sessions.pop(sid, None)
            return None
        return session

    def pop(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        return self._sessions.pop(sid, None)

    def _sweep(self) -> None:
        now = time.time()
        for state, attempt in list(self._logins.items()):
            if now - attempt.created > self.LOGIN_TTL:
                self._logins.pop(state, None)
        for sid, session in list(self._sessions.items()):
            if now - session.created > self._session_ttl:
                self._sessions.pop(sid, None)
