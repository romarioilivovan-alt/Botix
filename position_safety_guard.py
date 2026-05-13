import asyncio
import datetime as dt
import os
import time

os.environ.setdefault("ZFEE_CONFIG_PATH", r"C:\fluflip_work\code\config.json")

from backend.config import load_config
from backend.models import UserAccount
from backend.mexc_trader import MexcTrader


POLL_SEC = float(os.environ.get("SAFETY_POLL_SEC", "1.0"))
MAX_HOLD_SEC = float(os.environ.get("SAFETY_MAX_HOLD_SEC", "25.0"))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def pos_age_sec(pos: dict) -> float:
    raw = pos.get("createTime") or pos.get("create_time") or 0
    try:
        created = float(raw) / 1000.0
    except Exception:
        created = 0.0
    if created <= 0:
        return 0.0
    return max(0.0, time.time() - created)


async def main() -> None:
    cfg = load_config()
    acc = UserAccount(
        uid=cfg.mexc_web.web_uid.strip(),
        device_id=cfg.mexc_web.device_id.strip(),
        mhash=cfg.mexc_web.mhash.strip(),
        proxy=cfg.mexc_web.proxy,
        order_submit_path=cfg.mexc_web.order_submit_path,
    )
    trader = MexcTrader(acc, proxy=cfg.mexc_web.proxy)
    closing: set[int] = set()
    print(f"[guard] started {utc_now()} max_hold={MAX_HOLD_SEC:.1f}s", flush=True)
    try:
        while True:
            try:
                positions = await trader.get_positions_raw()
            except Exception as exc:
                print(f"[guard] positions error {utc_now()}: {exc!r}", flush=True)
                await asyncio.sleep(POLL_SEC)
                continue

            for pos in positions:
                try:
                    pid = int(pos.get("positionId") or 0)
                    symbol = str(pos.get("symbol") or "")
                    ptype = int(pos.get("positionType") or 0)
                    side = "LONG" if ptype == 1 else "SHORT"
                    vol = float(pos.get("holdVol") or pos.get("availableVol") or 0.0)
                    lev = int(float(pos.get("leverage") or 100))
                    age = pos_age_sec(pos)
                except Exception as exc:
                    print(f"[guard] bad position row {utc_now()}: {exc!r} {pos!r}", flush=True)
                    continue

                if not pid or vol <= 0:
                    continue
                if age < MAX_HOLD_SEC or pid in closing:
                    continue

                closing.add(pid)
                print(
                    f"[guard] stale position close {utc_now()} "
                    f"pid={pid} {symbol} {side} vol={vol} age={age:.1f}s",
                    flush=True,
                )
                try:
                    t0 = time.perf_counter()
                    res = await trader.close_reduce_only(
                        symbol,
                        side,
                        vol,
                        lev,
                        margin_mode=int(pos.get("openType") or 1),
                        position_id=pid,
                    )
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    print(
                        f"[guard] close result pid={pid} success={res.get('success')} "
                        f"latency_ms={elapsed:.1f} message={res.get('message')}",
                        flush=True,
                    )
                except Exception as exc:
                    closing.discard(pid)
                    print(f"[guard] close error pid={pid}: {exc!r}", flush=True)

            await asyncio.sleep(POLL_SEC)
    finally:
        await trader.close()


if __name__ == "__main__":
    asyncio.run(main())
