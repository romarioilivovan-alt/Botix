import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _load_cfg(path, monkeypatch):
    import backend.config as m

    monkeypatch.setattr(m, "CONFIG_PATH", pathlib.Path(path))
    return m.load_config()


def test_prepared_profile_configs_load(monkeypatch):
    root = pathlib.Path(__file__).resolve().parents[1]

    hype = _load_cfg(root / "config.paper_hype_solo_v1.json", monkeypatch)
    assert hype.mode == "paper"
    assert hype.port == 8115
    assert hype.universe.include_only == ["HYPE_USDT"]
    assert [ov.symbol for ov in hype.symbol_overrides] == ["HYPE_USDT"]

    ena = _load_cfg(root / "config.paper_ena_guarded_v2.json", monkeypatch)
    assert ena.mode == "paper"
    assert ena.port == 8117
    assert ena.universe.include_only == ["ENA_USDT"]
    assert ena.symbol_overrides[0].allow_short is True
    assert ena.symbol_overrides[0].allow_long is False

    mstr = _load_cfg(root / "config.paper_mstr_solo_v1.json", monkeypatch)
    assert mstr.mode == "paper"
    assert mstr.port == 8119
    assert mstr.universe.include_only == ["MSTRSTOCK_USDT"]
    assert mstr.symbol_overrides[0].algorithms == ["confluence"]

    tail = _load_cfg(root / "config.paper_tail_rebuild_v1.json", monkeypatch)
    assert tail.mode == "paper"
    assert tail.port == 8121
    assert set(tail.universe.include_only) == {
        "HYPE_USDT",
        "SUI_USDT",
        "PENGU_USDT",
        "UNI_USDT",
        "LINK_USDT",
    }
    overrides = {ov.symbol: ov for ov in tail.symbol_overrides}
    assert overrides["PENGU_USDT"].micro_levels == 3
    assert overrides["UNI_USDT"].taker_ioc_min_fill_ratio == 0.65


def test_profile_note_mentions_tail_rebuild():
    root = pathlib.Path(__file__).resolve().parents[1]
    note = json.dumps((root / "notes" / "profile_matrix_20260508.md").read_text(encoding="utf-8"))
    assert "config.paper_tail_rebuild_v1.json" in note
