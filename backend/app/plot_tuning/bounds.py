"""Default slider bounds for a numeric knob.

There is no semantic understanding here and there cannot be: a type-only scan
sees ``alpha = 0.5`` as a float and has no idea it must stay under 1. So the
heuristic is deliberately dumb — an order-ish of magnitude either side of the
current value — and the UI makes both bounds directly editable, widening rather
than clamping when a typed value falls outside. The heuristic is a starting
point, not a cage.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How far either side of the current value the default range reaches.
SCALE = 5


@dataclass(frozen=True)
class Bounds:
    minimum: float
    maximum: float
    step: float


def bounds_for(kind: str, value: float) -> Bounds | None:
    """Default range for a numeric knob, or None for types without a slider."""
    if kind == "int":
        if value == 0:
            # Zero has no scale to multiply, so pick a small usable range rather
            # than collapsing to a single point.
            low, high = 0, 10
        elif value > 0:
            low, high = int(value / SCALE), int(value * SCALE)
        else:
            low, high = int(value * SCALE), int(value / SCALE)
        if low == high:
            high = low + 1
        return Bounds(minimum=float(low), maximum=float(high), step=1.0)
    if kind == "float":
        if value == 0:
            low, high = 0.0, 1.0
        elif value > 0:
            low, high = value / SCALE, value * SCALE
        else:
            low, high = value * SCALE, value / SCALE
        return Bounds(minimum=low, maximum=high, step=(high - low) / 100)
    return None
