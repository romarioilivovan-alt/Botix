"""Capital allocator.

Decides whether to take an opportunity, and if so, sizes the position.
Enforces slot limits, per-symbol exclusivity, balance-fraction caps and
book-depth caps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .opportunity import Opportunity
from .state import AppState


@dataclass
class AllocationDecision:
    accept: bool
    reason: str = ""
    notional_usdt: float = 0.0
    margin_usdt: float = 0.0
    leverage: int = 1


class CapitalAllocator:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def decide(
        self,
        opp: Opportunity,
        state: AppState,
        *,
        balance_free: float,
        max_leverage_for_symbol: Optional[int],
        book_top_notional: float,
        margin_pct_override: Optional[float] = None,
        leverage_override: Optional[int] = None,
    ) -> AllocationDecision:
        # Kill switch / engine off
        if state.kill_switch:
            return AllocationDecision(False, "kill_switch")

        # Symbol already in position
        if opp.symbol in state.positions:
            return AllocationDecision(False, "already_open")

        # Per-symbol cooldown
        until = state.cooldown_until.get(opp.symbol, 0.0)
        if time.time() < until:
            return AllocationDecision(False, f"cooldown {until-time.time():.1f}s")

        # Slot count
        if len(state.positions) >= int(self.cfg.risk.max_concurrent_positions):
            return AllocationDecision(False, "no_slots")

        if balance_free < float(self.cfg.risk.min_balance_usdt):
            return AllocationDecision(False, "low_balance")

        # Margin per slot (per-symbol override takes precedence)
        margin_pct = margin_pct_override if margin_pct_override is not None else float(self.cfg.risk.margin_pct_per_slot)
        margin = balance_free * margin_pct
        if margin < float(self.cfg.risk.min_balance_usdt):
            return AllocationDecision(False, "tiny_margin")

        # Leverage (per-symbol override takes precedence)
        if leverage_override is not None:
            lev = int(leverage_override)
        elif str(self.cfg.risk.leverage_mode).lower() == "max":
            lev = int(max_leverage_for_symbol or self.cfg.risk.fixed_leverage)
        else:
            lev = int(self.cfg.risk.fixed_leverage)
        if lev <= 0:
            lev = 1

        notional = margin * lev

        # Cap by book depth
        cap = float(self.cfg.risk.book_depth_consume_pct) * float(book_top_notional or 0.0)
        if cap > 0 and notional > cap:
            notional = cap
            margin = notional / lev

        if notional <= 0 or margin <= 0:
            return AllocationDecision(False, "zero_size")

        return AllocationDecision(
            accept=True,
            notional_usdt=notional,
            margin_usdt=margin,
            leverage=lev,
        )
