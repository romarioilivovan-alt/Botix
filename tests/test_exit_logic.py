import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.paper import (
    _residual_edge_bps,
    _should_bad_entry_exit,
    _should_profit_protect_exit,
    _update_settled_profit_state,
)
from backend.state import ManagedPosition


def _pos(side: str = "LONG", entry_spread_bps=None) -> ManagedPosition:
    return ManagedPosition(
        symbol="PEPE_USDT",
        side=side,
        entry_price=100.0,
        notional_usdt=100.0,
        margin_usdt=10.0,
        leverage=10.0,
        qty=1.0,
        open_ts=0.0,
        fair_at_open=100.0,
        sigma_at_open=0.1,
        entry_spread_bps=entry_spread_bps,
    )


def test_residual_edge_bps_respects_side():
    long_pos = _pos("LONG")
    short_pos = _pos("SHORT")
    assert round(_residual_edge_bps(long_pos, 100.5, 100.2), 4) == round((0.3 / 100.5) * 1e4, 4)
    assert round(_residual_edge_bps(short_pos, 99.5, 99.8), 4) == round((0.3 / 99.5) * 1e4, 4)


def test_profit_protect_exit_needs_giveback_after_edge_collapse():
    assert _should_profit_protect_exit(
        current_bps=1.2,
        best_bps=2.1,
        residual_edge_bps=0.2,
        arm_bps=1.0,
        giveback_bps=0.6,
        fast_arm_bps=2.5,
        fast_giveback_bps=0.8,
        min_profit_bps=1.0,
        edge_collapse_bps=0.4,
    )


def test_profit_protect_stays_open_when_edge_is_still_strong():
    assert not _should_profit_protect_exit(
        current_bps=1.2,
        best_bps=2.1,
        residual_edge_bps=1.0,
        arm_bps=1.0,
        giveback_bps=0.6,
        fast_arm_bps=2.5,
        fast_giveback_bps=0.8,
        min_profit_bps=1.0,
        edge_collapse_bps=0.4,
    )


def test_settled_profit_state_requires_stable_profitable_hold():
    pos = _pos("LONG")
    ok, _ = _update_settled_profit_state(
        pos,
        now=10.0,
        current_bps=1.9,
        residual_edge_bps=0.3,
        hold_sec=0.65,
        min_bps=1.8,
        max_drift_bps=0.3,
        edge_bps=0.4,
    )
    assert not ok
    assert pos.settled_profit_since == 10.0

    ok, _ = _update_settled_profit_state(
        pos,
        now=10.4,
        current_bps=2.1,
        residual_edge_bps=0.3,
        hold_sec=0.65,
        min_bps=1.8,
        max_drift_bps=0.3,
        edge_bps=0.4,
    )
    assert not ok

    ok, reason = _update_settled_profit_state(
        pos,
        now=10.8,
        current_bps=2.0,
        residual_edge_bps=0.3,
        hold_sec=0.65,
        min_bps=1.8,
        max_drift_bps=0.3,
        edge_bps=0.4,
    )
    assert ok
    assert "settled profit" in reason


def test_bad_entry_exit_triggers_on_unfavorable_fill_after_edge_disappears():
    pos = _pos("LONG", entry_spread_bps=0.7)
    assert _should_bad_entry_exit(
        pos,
        age_sec=0.8,
        current_bps=-0.4,
        residual_edge_bps=0.2,
        guard_sec=6.0,
        min_age_sec=0.4,
        bad_entry_spread_bps=0.5,
        exit_bps=-0.1,
        edge_collapse_bps=0.4,
    )
