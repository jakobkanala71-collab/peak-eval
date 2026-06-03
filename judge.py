"""LLM judge — scores agent natural-language answers against ground-truth data.

The judge sees: the user's question, the peak (golden) SQL, the ground-truth
result CSV, the agent's SQL, the exec-match diff summary, and the agent's
methodology explanation. It scores three independent binary dimensions
(factual_accuracy, completeness, no_hallucination) and classifies the SQL-level
failure_category when execution_match fails.

Module-level counters track call count and token usage across a run; reset
them with reset_usage() at the start of each run.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pandas as pd
from anthropic import Anthropic

JUDGE_MODEL = "claude-sonnet-4-6"

# Guardrails
CALLS_PER_CASE = 1  # one structured-JSON call returns all 4 scores
MAX_CALLS_PER_RUN = 75  # headroom for ~50 case corpus + retries/ensemble
TRUNCATE_RESULT_TO_ROWS = 200  # head of result sent to judge if larger

# Categories the judge picks from when execution_match fails. Used for
# scorecard breakdowns ("agent is weak on filters" vs "agent is weak on joins").
FAILURE_CATEGORIES = (
    "missing_filter",
    "wrong_filter",
    "wrong_aggregation",
    "wrong_grouping",
    "wrong_join",
    "wrong_table",
    "wrong_columns",
    "time_window",
    "precision",
    "other",
    None,  # use when execution_match passed (no failure)
)

# Anthropic Sonnet pricing, USD per million tokens. Update if Anthropic re-prices.
PRICE_INPUT_PER_MTOK = 3.0
PRICE_OUTPUT_PER_MTOK = 15.0


JUDGE_PROMPT = """You are evaluating an AI analytics agent that answers business questions by producing SQL + a methodology explanation. The agent does NOT quote specific numbers in its explanation — that is by design. The user reads numerical values from the result table separately; the agent's text describes *what the SQL does* and *why it answers the question*.

Your job is to grade the methodology against the SQL, the data, and the question.

You will receive:
1. The user's question.
2. The ground-truth (peak) SQL — the canonical reference query.
3. The ground-truth result, as CSV (for context — do NOT penalize the explanation for not quoting these values).
4. The agent's SQL.
5. The exec-match diff — how the agent's result compares to the ground truth.
6. The agent's methodology explanation.

Score on three independent binary dimensions:

- "factual_accuracy" (0 or 1): Does the methodology accurately describe what the **agent's own SQL** actually does? Score 0 ONLY when the methodology makes a specific claim about an SQL operation (a join, filter, grouping, aggregation, table, or column condition) that does not match the agent's SQL — e.g., "uses NOT is_first_ride alone" when the SQL uses two different conditions, "grouped by month" when grouping is daily, "filtered where status='active'" when the SQL has no WHERE clause, or names the wrong table. Score 1 when every operation the methodology *specifically describes* matches the agent's SQL. CRITICAL: do NOT score 0 just because the agent's SQL is wrong vs peak — SQL correctness is exec_match's job. Do NOT score 0 because the methodology uses a domain term from the user's question (e.g., "active capacity", "completed rides", "returning members") whose semantic meaning the SQL fails to enforce — that's a missing_filter / wrong_filter SQL bug caught by exec_match + failure_category, not a methodology lie. Domain words echoing the question are metric labels, not factual claims about SQL operations, unless the methodology explicitly asserts them as SQL conditions ("filtered where is_valid = TRUE").

- "completeness" (0 or 1): Does the methodology address the question's dimensions and core computation? Score 0 ONLY when the methodology itself fails to cover the question — e.g., the user asked for a breakdown by X and Y but methodology only describes X, methodology answers a different question, or methodology is so vague ("I queried the database") it fails to describe the computation. Score 1 when methodology covers the question's dimensions and computation. CRITICAL: do NOT score 0 because the SQL fails to enforce some aspect the methodology mentioned — SQL correctness is exec_match's job, not completeness'.

- "no_hallucination" (0 or 1): Does the explanation stay within what's derivable from the SQL, the data, and the schema? Score 0 if it adds claims like trends ("rates have been declining"), industry comparisons, causal stories, recommendations, or any assertion that goes beyond describing the computation. Score 1 if it only describes what was computed.

Independent dimensions — do not double-count. A misstated filter is factual_accuracy=0. A missing dimension is completeness=0. An extra unsupported claim is no_hallucination=0.

When the SQLs differ (per the exec-match diff), call out the SQL-level difference in your reasoning so the user gets actionable feedback — even though the SQL bug itself doesn't affect any of the three scores (it's caught by execution_match).

Return ONLY valid JSON, no markdown code fences, no prose before or after, in exactly this shape:

Additionally, classify the SQL-level failure (when exec-match diff indicates results don't match) by setting "failure_category" to ONE of:

- "missing_filter": agent omits a WHERE clause / data-quality filter present in peak.
- "wrong_filter": agent's filter has different conditions or wrong values vs peak.
- "wrong_aggregation": agent uses a different aggregation function (COUNT vs SUM, AVG vs MEDIAN, etc.).
- "wrong_grouping": agent groups by different columns or different granularity (day vs month).
- "wrong_join": agent uses a different join (different tables joined, or different join keys/types).
- "wrong_table": agent queries a different table than peak.
- "wrong_columns": agent selects different columns or omits dimensions the user asked for.
- "time_window": agent's date range or time filter differs from peak's.
- "precision": values nearly match but differ in float precision / rounding.
- "other": doesn't fit the categories above.

If the exec-match diff says results match, set failure_category to null.

Return ONLY valid JSON, no markdown code fences, no prose before or after, in exactly this shape:
{{
  "factual_accuracy": 0 or 1,
  "completeness": 0 or 1,
  "no_hallucination": 0 or 1,
  "failure_category": one of the categories above, or null,
  "reasoning": "2-4 sentences. Cite specific SQL operations and explanation phrases. When SQLs differ, name the SQL difference."
}}

---
USER QUESTION:
{prompt}

PEAK SQL (ground truth):
{peak_sql}

PEAK RESULT (CSV, context only):
{peak_result_csv}

AGENT SQL:
{agent_sql}

EXEC-MATCH DIFF:
{exec_diff}

AGENT METHODOLOGY:
{agent_answer}
"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_call_count = 0
_total_input_tokens = 0
_total_output_tokens = 0


def reset_usage() -> None:
    global _call_count, _total_input_tokens, _total_output_tokens
    _call_count = 0
    _total_input_tokens = 0
    _total_output_tokens = 0


def get_usage() -> dict[str, Any]:
    cost = (
        _total_input_tokens * PRICE_INPUT_PER_MTOK
        + _total_output_tokens * PRICE_OUTPUT_PER_MTOK
    ) / 1_000_000
    return {
        "calls": _call_count,
        "input_tokens": _total_input_tokens,
        "output_tokens": _total_output_tokens,
        "cost_usd": cost,
    }


def format_usage(usage: dict[str, Any]) -> str:
    return (
        f"{usage['calls']} calls, "
        f"{usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens, "
        f"~${usage['cost_usd']:.4f}"
    )


def judge_prompt_hash() -> str:
    return hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest()[:8]


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            raise
        return json.loads(match.group(0))


def llm_judge(
    prompt: str,
    agent_answer: str,
    peak_result_df: pd.DataFrame,
    peak_sql: str,
    agent_sql: str,
    exec_diff_summary: str,
    client: Anthropic | None = None,
) -> dict[str, Any]:
    """Score an agent's answer against ground-truth data with the configured judge model.

    Sends both SQLs and the exec-match diff alongside the data so the judge
    can give actionable feedback that connects 'the explanation is bad' with
    'the SQL has X bug'.
    """
    global _call_count, _total_input_tokens, _total_output_tokens

    if _call_count >= MAX_CALLS_PER_RUN:
        raise RuntimeError(
            f"Refusing judge call: would exceed MAX_CALLS_PER_RUN ({MAX_CALLS_PER_RUN}). "
            f"If intentional, raise the constant in judge.py."
        )

    if client is None:
        client = Anthropic()

    # Truncate result CSV sent to the judge — exec_match still uses the full df.
    if len(peak_result_df) > TRUNCATE_RESULT_TO_ROWS:
        head_csv = peak_result_df.head(TRUNCATE_RESULT_TO_ROWS).to_csv(index=False)
        peak_result_csv = (
            f"{head_csv}\n[truncated to first {TRUNCATE_RESULT_TO_ROWS} of "
            f"{len(peak_result_df)} rows]\n"
        )
    else:
        peak_result_csv = peak_result_df.to_csv(index=False)

    user_message = JUDGE_PROMPT.format(
        prompt=prompt,
        peak_sql=peak_sql,
        peak_result_csv=peak_result_csv,
        agent_sql=agent_sql,
        exec_diff=exec_diff_summary,
        agent_answer=agent_answer,
    )

    _call_count += 1
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}],
    )
    _total_input_tokens += response.usage.input_tokens
    _total_output_tokens += response.usage.output_tokens

    parsed = _extract_json(response.content[0].text)
    raw_category = parsed.get("failure_category")
    failure_category = (
        raw_category if raw_category in FAILURE_CATEGORIES else None
    )
    return {
        "factual_accuracy": int(parsed["factual_accuracy"]),
        "completeness": int(parsed["completeness"]),
        "no_hallucination": int(parsed["no_hallucination"]),
        "failure_category": failure_category,
        "reasoning": str(parsed.get("reasoning", "")),
    }
