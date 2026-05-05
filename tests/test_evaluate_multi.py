import sys, pathlib, math
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from unittest.mock import patch
from backend.config import AppConfig, SymbolOverride
from backend.opportunity import OpportunityEngine
from backend.state import SymbolStats


def _engine():
    cfg = AppConfig()
    return OpportunityEngine(cfg)


def _stats():
    st = SymbolStats()
    st.fair = 1.0
    st.mexc_mid = 1.0
    return st


def _override(algos, mode):
    return SymbolOverride(symbol="ENA_USDT", algorithms=algos, algo_mode=mode)


def test_any_mode_picks_highest_score():
    eng = _engine()
    scores = {"meanrev": (0.8, "LONG"), "raw_momentum": (2.1, "SHORT")}

    def fake_evaluate(symbol, st, algo=None):
        sc, side = scores[algo]
        st.score = sc
        st.side_hint = side
        st.blocked_reason = None
        return st

    ov = _override(["meanrev", "raw_momentum"], "ANY")
    with patch.object(eng, "_evaluate_single", side_effect=fake_evaluate):
        result = eng.evaluate_multi("ENA_USDT", _stats(), ov)

    assert result.score == 2.1
    assert result.side_hint == "SHORT"
    assert result.blocked_reason is None


def test_any_mode_fires_on_single_signal():
    eng = _engine()

    def fake_evaluate(symbol, st, algo=None):
        if algo == "meanrev":
            st.score = 1.5
            st.side_hint = "LONG"
            st.blocked_reason = None
        else:
            st.score = 0.0
            st.side_hint = None
            st.blocked_reason = "no_signal"
        return st

    ov = _override(["meanrev", "raw_momentum"], "ANY")
    with patch.object(eng, "_evaluate_single", side_effect=fake_evaluate):
        result = eng.evaluate_multi("ENA_USDT", _stats(), ov)

    assert result.score == 1.5
    assert result.side_hint == "LONG"
    assert result.blocked_reason is None


def test_any_mode_blocked_when_no_signals():
    eng = _engine()

    def fake_evaluate(symbol, st, algo=None):
        st.score = 0.0
        st.blocked_reason = "no_signal"
        return st

    ov = _override(["meanrev", "raw_momentum"], "ANY")
    with patch.object(eng, "_evaluate_single", side_effect=fake_evaluate):
        result = eng.evaluate_multi("ENA_USDT", _stats(), ov)

    assert result.score == 0.0
    assert result.blocked_reason is not None


def test_best_mode_requires_threshold():
    eng = _engine()

    def fake_evaluate(symbol, st, algo=None):
        st.score = 1.0  # below threshold 1.2
        st.side_hint = "LONG"
        st.blocked_reason = None
        return st

    ov = _override(["meanrev"], "BEST")
    with patch.object(eng, "_evaluate_single", side_effect=fake_evaluate):
        result = eng.evaluate_multi("ENA_USDT", _stats(), ov)

    assert result.blocked_reason == "best_below_threshold"


def test_best_mode_passes_threshold():
    eng = _engine()

    def fake_evaluate(symbol, st, algo=None):
        st.score = 1.5  # above threshold 1.2
        st.side_hint = "LONG"
        st.blocked_reason = None
        return st

    ov = _override(["meanrev"], "BEST")
    with patch.object(eng, "_evaluate_single", side_effect=fake_evaluate):
        result = eng.evaluate_multi("ENA_USDT", _stats(), ov)

    assert result.score == 1.5
    assert result.blocked_reason is None


def test_consensus_requires_all_agree():
    eng = _engine()
    scores = {"meanrev": ("LONG", 1.5), "raw_momentum": ("SHORT", 1.2)}

    def fake_evaluate(symbol, st, algo=None):
        side, sc = scores[algo]
        st.score = sc
        st.side_hint = side
        st.blocked_reason = None
        return st

    ov = _override(["meanrev", "raw_momentum"], "CONSENSUS")
    with patch.object(eng, "_evaluate_single", side_effect=fake_evaluate):
        result = eng.evaluate_multi("ENA_USDT", _stats(), ov)

    assert result.blocked_reason == "consensus_conflict"


def test_consensus_passes_when_all_agree():
    eng = _engine()
    scores = {"meanrev": ("LONG", 1.5), "raw_momentum": ("LONG", 1.2)}

    def fake_evaluate(symbol, st, algo=None):
        side, sc = scores[algo]
        st.score = sc
        st.side_hint = side
        st.blocked_reason = None
        return st

    ov = _override(["meanrev", "raw_momentum"], "CONSENSUS")
    with patch.object(eng, "_evaluate_single", side_effect=fake_evaluate):
        result = eng.evaluate_multi("ENA_USDT", _stats(), ov)

    assert result.blocked_reason is None
    assert result.side_hint == "LONG"
    assert abs(result.score - math.sqrt(1.5 * 1.2)) < 1e-9


def test_evaluate_multi_fallback_when_no_algorithms():
    """When override.algorithms is None/empty, falls back to global evaluate()."""
    eng = _engine()
    ov = SymbolOverride(symbol="ENA_USDT", algorithms=None, algo_mode="ANY")
    st = _stats()
    # Should not raise — falls back to evaluate()
    result = eng.evaluate_multi("ENA_USDT", st, ov)
    assert result is st
