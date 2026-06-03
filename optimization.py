"""Compare agent vs peak query efficiency (deterministic, no LLM).

Optimization is scored on **bytes scanned only**, not on execution time.
Reason: BigQuery duration is noisy (slot allocation, queue wait, network)
and varies meaningfully run-to-run for the same query. Bytes scanned is
deterministic — it reflects the work the query actually requires. A query
that's "faster" without scanning less data isn't a real optimization.

Optimization is only meaningful when results match (`exec_match=True`).
If results differ, the queries aren't doing the same thing — comparing
efficiency is apples-to-oranges.
"""

from __future__ import annotations

from typing import Literal

EFFICIENCY_THRESHOLD = 0.10  # ±10% on bytes scanned before flagging better/worse

OptimizationScore = Literal["better", "comparable", "worse", "n/a"]


def optimization_score(
    *,
    peak_bytes: int,
    peak_duration_ms: float,  # kept for signature stability; unused in scoring
    agent_bytes: int,
    agent_duration_ms: float,  # kept for signature stability; unused in scoring
    exec_match: bool,
) -> OptimizationScore:
    """Compare agent's query efficiency against peak's, by bytes scanned.

    "n/a"        — exec_match=False (results differ; can't fairly compare efficiency)
    "better"     — agent scans ≥10% fewer bytes
    "worse"      — agent scans ≥10% more bytes
    "comparable" — within ±10% bytes
    """
    if not exec_match:
        return "n/a"
    if peak_bytes == 0:
        return "comparable"

    bytes_ratio = (agent_bytes - peak_bytes) / peak_bytes

    if bytes_ratio <= -EFFICIENCY_THRESHOLD:
        return "better"
    if bytes_ratio >= EFFICIENCY_THRESHOLD:
        return "worse"
    return "comparable"


def should_review_peak_sql(
    *,
    exec_match: bool,
    judge: dict | None,
    peak_bytes: int,
    peak_duration_ms: float = 0,  # kept for signature stability; not used
    agent_bytes: int,
    agent_duration_ms: float = 0,  # ditto
) -> tuple[bool, str]:
    """Flag cases where peak's golden SQL might be improvable.

    Trigger (deliberately conservative — only the unambiguous win):
      - exec_match=True   (agent produces the same data as peak, so we know
                           the SQLs are doing the same job)
      - judge=(1,1,1)     (methodology is faithful, addresses the question,
                           no hallucinations)
      - bytes scanned by agent ≥10% lower than peak (real efficiency win)

    If results differ at all, we trust peak's golden query — the agent might
    be skipping necessary work. Better to miss a true improvement than to
    surface false positives that need manual triage.
    """
    if not exec_match:
        return False, "results differ — trust peak"
    if not judge:
        return False, "no judge result"
    if not (
        judge.get("factual_accuracy") == 1
        and judge.get("completeness") == 1
        and judge.get("no_hallucination") == 1
    ):
        return False, "judge flagged the methodology"
    if peak_bytes == 0:
        return False, "peak stats unavailable"

    bytes_ratio = (agent_bytes - peak_bytes) / peak_bytes
    if bytes_ratio <= -EFFICIENCY_THRESHOLD:
        return True, (
            f"results match, methodology is sound, agent scans "
            f"{bytes_ratio:+.0%} bytes vs peak — real optimization win, "
            f"peak_sql may benefit from the same approach"
        )
    return False, "agent isn't meaningfully more efficient"
