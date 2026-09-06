import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """
    print 대신 표준 logging을 쓴다.
    컨테이너 로그 수집기가 파싱할 수 있도록 stdout으로 한 줄씩 내보낸다.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
