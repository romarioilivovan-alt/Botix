from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class UserAccount:
    """Minimal account model used by MexcFuturesAPI."""

    uid: str
    # Optional client fingerprint for MEXC web endpoints.
    device_id: str = ""
    # Optional MEXC web anti-bot token (mhash). If empty, we derive a stable value.
    mhash: str = ""
    # Static client hash used by MEXC web (seen in their JS bundle).
    chash: str = "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8"
    default_size: float = 50.0
    default_leverage: int = 5
    margin_mode: int = 1  # 1=cross, 2=isolated
    proxy: Optional[str] = None
