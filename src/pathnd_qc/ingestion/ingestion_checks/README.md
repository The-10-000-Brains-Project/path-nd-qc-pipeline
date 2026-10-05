# Ingestion checks

[Package overview](../../README.md) · [CLI usage](../../../USAGE_CLI.md) · [Library usage](../../../USAGE_LIBRARY.md) · [Parent module](../README.md)

[Resolution and magnification](#resolution-and-magnification) · [Integrity and decisions](#integrity-and-decisions) · [Developer functions](#developer-functions) · [Scale observations](#scale-observations)

These checks describe acquisition quality and whether a slide has usable scale information. They
are recorded under `m1` in the pipeline report. A recorded check, a decision recommendation, and a
fatal pipeline error are different; review all three alongside `provenance.execution`.

## Resolution and magnification

`check_magnification(info)` checks the objective label against a minimum, default 20×. The tile
pipeline uses effective microns per pixel (MPP), rather than this label, to gate tile work.
A smaller MPP means finer spatial resolution.

`check_mpp(info)` determines a usable scale in this order:

1. Use stated X/Y MPP when both fall in the configured plausible range (default 0.1–10.0).
2. Accept positive values below the lower limit when both are consistent with objective power
   through the configured MPP × objective range (default 5–15).
3. Otherwise derive MPP as `10 / objective_power` if that is plausible.
4. If none works, mark the scale implausible.

The effective scale passes the default tile gate at MPP ≤ `0.5 + 0.05`. The target comes from
`m3.read.tile_target_mpp`, with `ingestion.mpp_tolerance_abs` slack. A plausible but coarser slide
can still support M2/M4; an implausible scale prevents the pipeline's M2 plane read unless a suitable
image is supplied. See [resolution rules](../../../USAGE_CLI.md#7-resolution-and-downloads).

`apply_scale(info, check)` gives downstream reads the checked/derived scale while preserving raw
acquisition values in the record. The pipeline assumes square pixels for its working geometry;
this is not a full calibration or anisotropy validation.

## Integrity and decisions

`check_integrity(slide, info)` checks structure and decodes a bounded sample: a small pyramid level
(or a bounded center read), plus spot regions. Defaults are a 4,194,304-pixel decode cap, three spot
regions and 256-pixel spot size. It does not decode every tile or prove the entire file is sound.
Download transfer verification is a separate reader responsibility.

`build_ingestion_record(source, info, checks)` records source, acquisition, checks, version and
timestamps. `decide(record, enforce=False)` recommends quarantine for failed integrity and otherwise
flags failed checks. Programmatic enforcement with both `enforce=True` and an `active_policy`
can reject other check failures. The main pipeline
records the decision but does not automatically abort solely because it says quarantine; its
resolution gates operate separately. No CLI flag exposes `enforce`.

## Developer functions

| Function | Use |
|---|---|
| `check_magnification`, `check_mpp`, `check_integrity` | Produce individual check results |
| `apply_scale` | Apply checked scale to reader information |
| `build_ingestion_record` | Assemble an ingestion record |
| `decide` | Return a decision and flags |
| `to_report` | Convert the record to the M1 report section |
| `write_record` | Write a standalone ingestion record |
| `ingest_slide` | Older row/adapter-driven helper; not the main pipeline path |

The pipeline embeds the record in its report and does not use the legacy adapter-based helper.
See [legacy adapter limitations](../adapters/README.md) before calling that helper directly.
Defaults are in [configuration](../../config/defaults.json) under `ingestion.*`, plus the M3 target.

## Scale observations

`check_mpp.observations` records `mpp_axis_disagreement` when stated positive X/Y spacing
differs beyond numerical rounding (relative tolerance 1e-6), and `scale_disagreement` when
stated X spacing times objective power falls outside the inclusive configured product window.
These are factual metadata observations: they do not replace plausible stated spacing or
change the resolution gate. Processing still assumes square pixels. Product 5.0 is inside
the default [5,15] window; product 20.0 is outside.
