"""Halt detection for the entry guard — pure decision, separately testable.

A real LULD trading halt isn't directly readable on the paper/IEX feed, so we
infer it from a gap in a symbol's minute bars: during RTH a liquid gapper that
goes silent for minutes while the rest of the book keeps printing is almost
certainly halted (you can't exit during a halt, and it gaps through the stop on
resume). The bar-age math lives in the loop (it queries the research DB); this
module is just the decision so its edge cases are unit-tested.
"""

from __future__ import annotations


def is_halt_gap(
    sym_age_s: float | None,
    global_age_s: float | None,
    gap_seconds: float,
) -> bool:
    """True iff this symbol looks halted: its freshest bar is older than
    ``gap_seconds`` WHILE ingestion is globally healthy (the freshest bar across
    ALL names is within ``gap_seconds``). The global-health gate is what stops a
    feed-wide lag from flagging the entire book at once.

    ``None`` ages (no data) deliberately return False — NOT halted. This fails
    SAFE: a query error or a session-date mismatch makes the guard inert (entries
    proceed under the other gates) rather than mass-blocking every entry. The
    cost of the rare missed halt is bounded by the extension ceiling, which still
    blocks the gap-through on resume.
    """
    if gap_seconds <= 0:
        return False
    if sym_age_s is None or sym_age_s <= gap_seconds:
        return False
    return global_age_s is not None and global_age_s <= gap_seconds
