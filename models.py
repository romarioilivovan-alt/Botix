from dataclasses import dataclass
from typing import Optional


@dataclass
class UserAccount:
    uid: str
    account_name: str
    default_size: float = 50.0
    default_leverage: int = 5
    margin_mode: int = 1
    proxy: Optional[str] = None
