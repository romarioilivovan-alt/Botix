# Profile Matrix (2026-05-08)

This note keeps the non-core profile work separate from the ready core live configs.

## Kept intact

- `config.real_lineA_contract_v2.json`
- `config.real_lineB_hybrid_v1.json`

## Paper-ready candidates

- `config.paper_hype_solo_v1.json`
  - Derived from the strongest HYPE-positive branch.
  - HYPE is the cleanest non-core add-on so far.
  - Current smoke (`2026-05-08`): positive on current market.

- `config.paper_ena_guarded_v2.json`
  - Short-only ENA raw-momentum profile.
  - Built from the positive ENA runs, but with tighter risk and better exit laddering.
  - Current smoke (`2026-05-08`): positive on current market.

- `config.paper_mstr_solo_v1.json`
  - MSTR-only stock profile.
  - NVIDIA is excluded because its historical expectancy was decisively negative.
  - Current smoke (`2026-05-08`): still around flat-to-slightly-negative, so keep it exploratory.

## Experimental rebuild

- `config.paper_tail_rebuild_v1.json`
  - Sandbox for `SUI`, `PENGU`, `UNI`, `LINK`, plus `HYPE`.
  - Uses tighter age/drift/fill filters and microstructure gating.
  - This file is for paper proof only, not live promotion.
  - Current smoke (`2026-05-08`): `HYPE` and `SUI` both traded slightly positive after lowering the technical `tiny_margin` floor.

## Historical status by symbol

- `HYPE_USDT`: positive across multiple runs; best tail candidate.
- `ENA_USDT`: mixed, but salvageable in short-only raw-momentum form.
- `MSTRSTOCK_USDT`: modestly positive when isolated from NVIDIA.
- `SUI_USDT`: closest to breakeven among the weak tail names.
- `UNI_USDT`: still negative; only keep in experimental rebuild.
- `LINK_USDT`: still negative; only keep in experimental rebuild.
- `PENGU_USDT`: still negative overall, but close enough to justify one strict rebuild pass.
- `NVIDIA_USDT`: consistently negative; not promoted.
