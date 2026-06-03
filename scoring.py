"""Deterministic scorers — compare two query results."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class ExecDiff:
    """Human-readable diagnostic of how two result dataframes differ."""

    matched: bool
    summary: str  # one-line headline ("identical", "5 extra rows in agent", etc.)
    detail: str  # multi-line: column diff, row count delta, sample value mismatches


def diff_results(peak_df: pd.DataFrame, agent_df: pd.DataFrame) -> ExecDiff:
    """Produce a human-readable diff between two result dataframes.

    Captures the kind of differences peak benchmark cares about:
    - Column set mismatch (different schema)
    - Row count mismatch (one side missing/extra rows)
    - Value mismatches (within shared rows after sorting)
    """
    peak_cols = list(peak_df.columns)
    agent_cols = list(agent_df.columns)

    if peak_cols != agent_cols:
        only_peak = [c for c in peak_cols if c not in agent_cols]
        only_agent = [c for c in agent_cols if c not in peak_cols]
        detail_lines = []
        if only_peak:
            detail_lines.append(f"missing in agent: {only_peak}")
        if only_agent:
            detail_lines.append(f"extra in agent: {only_agent}")
        return ExecDiff(
            matched=False,
            summary=f"column mismatch (peak: {peak_cols}, agent: {agent_cols})",
            detail="\n".join(detail_lines),
        )

    n_peak, n_agent = len(peak_df), len(agent_df)
    if n_peak != n_agent:
        delta = n_agent - n_peak
        sign = "+" if delta > 0 else ""
        return ExecDiff(
            matched=False,
            summary=f"row count differs: peak={n_peak}, agent={n_agent} ({sign}{delta})",
            detail=f"agent returned {abs(delta)} {'extra' if delta > 0 else 'fewer'} rows",
        )

    # Same columns + same row count — check values via sorted comparison.
    sort_cols = peak_cols
    p = peak_df.sort_values(sort_cols).reset_index(drop=True)
    a = agent_df.sort_values(sort_cols).reset_index(drop=True)

    try:
        pd.testing.assert_frame_equal(p, a, check_exact=False, check_dtype=False)
        return ExecDiff(matched=True, summary="results match", detail="")
    except AssertionError as e:
        # Find rows that differ for a sample
        compare = p.compare(a) if p.shape == a.shape else None
        sample = ""
        if compare is not None and not compare.empty:
            sample = compare.head(5).to_string()
        return ExecDiff(
            matched=False,
            summary=f"values differ in {len(p)} rows ({len(p) - (p == a).all(axis=1).sum()} mismatched)",
            detail=f"first mismatching rows:\n{sample}\n\nassert_frame_equal: {e}".strip(),
        )


def execution_match(peak_df: pd.DataFrame, agent_df: pd.DataFrame) -> bool:
    """Return True iff the two dataframes are equal as multi-sets of rows.

    Sort-invariant (sorts both by all columns before comparing) and
    float-tolerant (uses pandas' default rtol/atol via assert_frame_equal
    with check_exact=False). Column order matters: different columns means
    different answer. dtype is checked loosely so int/float coercion from
    the warehouse doesn't cause spurious mismatches.
    """
    if list(peak_df.columns) != list(agent_df.columns):
        return False
    if len(peak_df) != len(agent_df):
        return False

    sort_cols = list(peak_df.columns)
    p = peak_df.sort_values(sort_cols).reset_index(drop=True)
    a = agent_df.sort_values(sort_cols).reset_index(drop=True)

    try:
        pd.testing.assert_frame_equal(p, a, check_exact=False, check_dtype=False)
    except AssertionError:
        return False
    return True
