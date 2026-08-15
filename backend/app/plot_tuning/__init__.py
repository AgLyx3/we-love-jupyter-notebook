"""Interactive plot tuning: find the literals a plot depends on, and rewrite them.

Design: docs/plans/2026-08-14-interactive-plot-tuning.md

This package is deliberately split so that the part that can be *wrong* is pure.
`discovery`, `rewrite` and `bounds` touch no kernel, no document service and no
API — they take source strings and return data. The correctness boundary of the
whole feature (which literals may be offered as knobs) lives here.
"""
