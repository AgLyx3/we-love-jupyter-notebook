# Overview panel probes

Throwaway-quality scripts kept because they carry evidence, not because they are
the implementation. They exist to answer one question the design could not
answer by argument: **can a model segment a messy notebook better than static
analysis can?**

See `../2026-08-22-notebook-overview-research.md` §13 for the results and
`../2026-08-22-notebook-overview-spec.md` for what to build.

## Fixtures

| Notebook | Shape | Why |
|---|---|---|
| `messy-exploration.ipynb` | 57 cells · 0 headings · 0 defs · out-of-order · 2 never run | The hard case. Both annotation layers dark, so generated names carry the map alone. |
| `simulation-sweep.ipynb` | 21 cells · 1 heading · 7 defs · in order | The contrast. Def-heavy research code, where the function layer does real work. |

## Running

```bash
python3 extract.py *.ipynb                      # Tier 0/1 only — no model, no network
python3 segment.py messy-exploration.ipynb      # model segmentation + validation
python3 segment.py --model haiku <notebook>
```

`segment.py` requires the `claude` CLI on `PATH`. It shells out directly, which
is right for a probe and wrong for the app — the real implementation goes
through `agent_workspace/adapters.py` (spec §4.2).

## Results, for reference

| | Deterministic | Model (default) | Model (Haiku) |
|---|---|---|---|
| Blocks on the hard case | 5 | 17 | 14 |
| Largest block | **44 cells** | 7 | 9 |
| Valid partition | — | first try | first try |
