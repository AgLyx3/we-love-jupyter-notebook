"""Per-operation ledger for an applied agent turn.

An agent turn applies a set of cell source changes atomically. This module
splits each of those changes into individually reviewable **operations** (one
per diff hunk) so a user can keep or undo them one at a time, and rebuilds a
cell's source from the pre-turn source plus the current per-operation states.

Two properties the rest of the system relies on:

- ``SourceHunk`` stores **index ranges only**, never copies of the line text.
  The sources are already retained on the turn's changes, so the ledger costs a
  few ints per hunk against a bounded turn-history budget. Do not "optimise"
  this by caching materialised hunk text on the operation.
- ``compose`` is a pure function of the pre-turn source and the operation
  states. The cell's expected content is therefore always recomputable, the
  ledger cannot drift from the document, and rejecting is reversible by
  flipping a state back rather than by storing an inverse patch.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Iterable, Sequence


# Mirrors frontend/src/notebook/cellDiff.ts so both ends fall back to the coarse
# diff on exactly the same inputs. A cell that trips any bound yields a single
# whole-cell operation, which is the right behaviour anyway: a 600-line rewrite
# is not usefully reviewable hunk by hunk.
MAX_DIFF_CHARS = 120_000
MAX_DIFF_LINES = 600
MAX_DIFF_WORK = 40_000

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
#: Operations in these states still describe content the turn put in the cell.
APPLIED_STATES = frozenset({PENDING, ACCEPTED})

KIND_SOURCE_HUNK = "source_hunk"


@dataclass(frozen=True)
class SourceHunk:
    """One contiguous divergence between a cell's pre-turn and post-turn source.

    Both ranges are half-open and index the *line arrays* of those sources.
    A pure insertion has ``prev_start == prev_end``; a pure deletion has
    ``next_start == next_end``.
    """

    ordinal: int
    prev_start: int
    prev_end: int
    next_start: int
    next_end: int


@dataclass(frozen=True)
class TurnOperation:
    operation_id: str
    cell_id: str
    ordinal: int
    hunk: SourceHunk
    kind: str = KIND_SOURCE_HUNK
    state: str = PENDING


def operation_id(turn_id: str, cell_id: str, ordinal: int) -> str:
    """Deterministic, so a client refetching a truncated turn keeps stable IDs."""
    return f"{turn_id}:{cell_id}:{ordinal}"


def split_lines(source: str) -> list[str]:
    return source.split("\n")


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def diff_hunks(previous_source: str, next_source: str) -> tuple[SourceHunk, ...]:
    """Split a source change into contiguous hunks of index ranges."""
    previous = split_lines(previous_source)
    following = split_lines(next_source)
    if previous == following:
        return ()
    if _exceeds_diff_bounds(previous_source, next_source, previous, following):
        return _coarse_hunks(previous, following)
    return _lcs_hunks(previous, following)


def _exceeds_diff_bounds(
    previous_source: str, next_source: str,
    previous: Sequence[str], following: Sequence[str],
) -> bool:
    return (
        len(previous_source) + len(next_source) > MAX_DIFF_CHARS
        or len(previous) + len(following) > MAX_DIFF_LINES
        or len(previous) * len(following) > MAX_DIFF_WORK
    )


def _coarse_hunks(
    previous: Sequence[str], following: Sequence[str],
) -> tuple[SourceHunk, ...]:
    """Trim the common prefix and suffix and call the rest one hunk."""
    prefix = 0
    while (
        prefix < len(previous) and prefix < len(following)
        and previous[prefix] == following[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(previous) - prefix and suffix < len(following) - prefix
        and previous[len(previous) - 1 - suffix] == following[len(following) - 1 - suffix]
    ):
        suffix += 1
    return (
        SourceHunk(
            ordinal=0,
            prev_start=prefix, prev_end=len(previous) - suffix,
            next_start=prefix, next_end=len(following) - suffix,
        ),
    )


def _lcs_hunks(
    previous: Sequence[str], following: Sequence[str],
) -> tuple[SourceHunk, ...]:
    """Longest-common-subsequence diff, grouped into maximal changed runs.

    The walk mirrors ``lineDiff`` in cellDiff.ts, including its tie-breaking, so
    hunk boundaries agree with what the editor would have drawn.
    """
    rows, columns = len(previous), len(following)
    table = [[0] * (columns + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(columns - 1, -1, -1):
            table[i][j] = (
                table[i + 1][j + 1] + 1 if previous[i] == following[j]
                else max(table[i + 1][j], table[i][j + 1])
            )

    hunks: list[SourceHunk] = []
    i = j = 0
    while i < rows or j < columns:
        if i < rows and j < columns and previous[i] == following[j]:
            i += 1
            j += 1
            continue
        start_i, start_j = i, j
        while i < rows or j < columns:
            if i < rows and j < columns and previous[i] == following[j]:
                break
            if j < columns and (i == rows or table[i][j + 1] >= table[i + 1][j]):
                j += 1
            else:
                i += 1
        hunks.append(
            SourceHunk(
                ordinal=len(hunks),
                prev_start=start_i, prev_end=i,
                next_start=start_j, next_end=j,
            )
        )
    return tuple(hunks)


def build_operations(
    *, turn_id: str, cell_id: str, previous_source: str, next_source: str,
) -> tuple[TurnOperation, ...]:
    return tuple(
        TurnOperation(
            operation_id=operation_id(turn_id, cell_id, hunk.ordinal),
            cell_id=cell_id, ordinal=hunk.ordinal, hunk=hunk,
        )
        for hunk in diff_hunks(previous_source, next_source)
    )


def compose(
    *, previous_source: str, next_source: str,
    operations: Iterable[TurnOperation],
) -> str:
    """Rebuild a cell's source from its pre-turn source and per-operation states.

    An operation still applied (``pending`` or ``accepted``) contributes the
    turn's lines; a ``rejected`` one contributes the pre-turn lines instead. All
    operations applied reproduces ``next_source`` exactly; all rejected
    reproduces ``previous_source`` exactly.
    """
    previous = split_lines(previous_source)
    following = split_lines(next_source)
    ordered = sorted(operations, key=lambda item: item.ordinal)
    composed: list[str] = []
    cursor = 0
    for operation in ordered:
        hunk = operation.hunk
        composed.extend(previous[cursor:hunk.prev_start])
        if operation.state == REJECTED:
            composed.extend(previous[hunk.prev_start:hunk.prev_end])
        else:
            composed.extend(following[hunk.next_start:hunk.next_end])
        cursor = hunk.prev_end
    composed.extend(previous[cursor:])
    return "\n".join(composed)


def with_state(
    operations: Sequence[TurnOperation], target_id: str, state: str,
) -> tuple[TurnOperation, ...]:
    return tuple(
        replace(operation, state=state)
        if operation.operation_id == target_id else operation
        for operation in operations
    )


def is_stale(
    *, current_source: str, previous_source: str, next_source: str,
    operations: Sequence[TurnOperation],
) -> bool:
    """Whether the cell has drifted from what the ledger says it should be.

    Derived rather than stored: a manual edit reaches the document through a
    path that knows nothing about the ledger, so a stored flag would only become
    true the next time someone attempted a reject — leaving live-looking
    controls on operations that are already dead.
    """
    expected = compose(
        previous_source=previous_source, next_source=next_source,
        operations=operations,
    )
    return source_hash(current_source) != source_hash(expected)
