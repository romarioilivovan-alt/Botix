"""Entry point for running the bot."""
import uvicorn

from backend.config import load_config

if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run(
        "backend.app:app",
        host=cfg.host,
        port=int(cfg.port),
        reload=False,
        log_level="info"
    )
