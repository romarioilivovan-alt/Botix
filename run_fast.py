"""Fast entry point â€” no uvicorn, pure asyncio.
Uses the same engine/aggregator/opportunity/executor but without HTTP server overhead.
This gives ~3-5x faster signal-to-submit latency.
"""
import asyncio
import logging
import signal
import sys
import os

# TCP optimizations before any imports that create sockets
if sys.platform == "win32":
    # Windows high-resolution timer
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

from backend.config import load_config
from backend.engine import Engine
from backend.state import AppState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("run_fast")


async def main():
    cfg = load_config()
    state = AppState()
    engine = Engine(cfg, state)

    # Start engine (connects WS, starts scoring loop, executor)
    await engine.start()

    logger.info(
        "Fast bot started: mode=%s symbols=%s tick=%sms",
        cfg.mode,
        cfg.universe.include_only or "auto",
        int(float(cfg.strategy.paper_tick_sec) * 1000),
    )

    # Run until interrupted
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler
            pass

    # On Windows, use keyboard interrupt
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass

    logger.info("Shutting down...")
    await engine.shutdown()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
