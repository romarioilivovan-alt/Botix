import copy
import json
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]

LIVE_ONLY_TOP_LEVEL = {
    "mode",
    "mexc_web",
    "reference_exchanges",
    "zero_fee_symbols",
    "host",
    "port",
}


def _load_json(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _normalized_strategy_payload(data: dict) -> dict:
    normalized = copy.deepcopy(data)
    for key in LIVE_ONLY_TOP_LEVEL:
        normalized.pop(key, None)
    return normalized


def _assert_live_mirrors_paper(*, paper_name: str, live_name: str) -> None:
    paper = _load_json(paper_name)
    live = _load_json(live_name)

    assert paper["mode"] == "paper"
    assert live["mode"] == "real"
    assert live["mexc_web"] == {
        "web_uid": "",
        "device_id": "",
        "mhash": "",
        "proxy": None,
    }
    assert live["reference_exchanges"] == ["binance"]
    assert live["zero_fee_symbols"] == []
    assert _normalized_strategy_payload(live) == _normalized_strategy_payload(paper)


def test_real_line_a_mirrors_alpha_core_no_zec() -> None:
    _assert_live_mirrors_paper(
        paper_name="config.paper_alpha_core_no_zec_v1.json",
        live_name="config.real_lineA_contract_v2.json",
    )


def test_real_line_b_mirrors_alpha_fusion() -> None:
    _assert_live_mirrors_paper(
        paper_name="config.paper_alpha_fusion_v1.json",
        live_name="config.real_lineB_hybrid_v1.json",
    )
