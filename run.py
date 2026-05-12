"""Entry point for running the bot.

On Linux/macOS we try to install uvloop for a 2-4x asyncio speedup.
On Windows uvloop is unavailable and we fall back to the default selector
loop.
"""
import logging
import sys

try:
    import uvloop  # type: ignore
    uvloop.install()
    _UV = True
except Exception:
    _UV = False

import uvicorn

from backend.config import load_config


def _log_startup_env() -> None:
    logger = logging.getLogger("startup")
    logger.info(
        "Python %s.%s.%s, uvloop=%s, platform=%s",
        sys.version_info.major, sys.version_info.minor, sys.version_info.micro,
        _UV, sys.platform,
    )


if __name__ == "__main__":
    _log_startup_env()
    cfg = load_config()
    # Tell uvicorn to prefer uvloop when available so the scoring loop and
    # WS ingress are scheduled with the faster event loop.
    loop_impl = "uvloop" if _UV else "auto"
    uvicorn.run(
        "backend.app:app",
        host=cfg.host,
        port=int(cfg.port),
        reload=False,
        log_level="info",
        loop=loop_impl,
        http="h11",  # h11 is the most stable; httptools can race on Windows
        timeout_keep_alive=65,
    )
