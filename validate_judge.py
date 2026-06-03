"""Calibrate the LLM judge against hand-scored synthetic cases.

Runs the judge on 10 cases with known scores, reports per-dimension agreement.
Use this when iterating JUDGE_PROMPT in judge.py — agreement should be ≥85%.

Self-contained: synthetic CSVs are inline, no BigQuery required.
Output is saved to runs/calibration_<timestamp>.md.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from typing import TypedDict

import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv
from tabulate import tabulate

from judge import (
    JUDGE_MODEL,
    format_usage,
    get_usage,
    judge_prompt_hash,
    llm_judge,
    reset_usage,
)
from report import utc_timestamp, write_run
from spinner import spinner

load_dotenv()

AGREEMENT_THRESHOLD = 0.85  # raw agreement bar (directional at small N)
KAPPA_SUBSTANTIAL = 0.60     # Landis-Koch: substantial inter-rater agreement
KAPPA_ALMOST_PERFECT = 0.80  # Landis-Koch: almost-perfect agreement
DIMENSIONS = ("factual_accuracy", "completeness", "no_hallucination")
CALIBRATION_DIR = Path(__file__).resolve().parent / "runs" / "calibration"


def cohens_kappa(y_true: list[int], y_pred: list[int]) -> float:
    """Cohen's κ for binary categorical agreement.

    Subtracts out chance agreement given the class balance, so it doesn't
    inflate when one class dominates. κ = 1 means perfect agreement; κ = 0
    means agreement at the rate chance alone would produce; κ < 0 means
    worse than chance.

    Landis-Koch interpretation: <0.2 poor · 0.2–0.4 fair · 0.4–0.6 moderate ·
    0.6–0.8 substantial · 0.8–1.0 almost perfect.
    """
    n = len(y_true)
    if n == 0:
        return 0.0
    po = sum(1 for t, p in zip(y_true, y_pred) if t == p) / n
    p_true_1 = sum(1 for t in y_true if t == 1) / n
    p_pred_1 = sum(1 for p in y_pred if p == 1) / n
    pe = p_true_1 * p_pred_1 + (1 - p_true_1) * (1 - p_pred_1)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def kappa_label(k: float) -> str:
    if k < 0.20: return "poor"
    if k < 0.40: return "fair"
    if k < 0.60: return "moderate"
    if k < 0.80: return "substantial"
    return "almost perfect"


class ValidationCase(TypedDict):
    id: str
    prompt: str
    peak_sql: str
    agent_sql: str
    peak_result_csv: str
    agent_answer: str  # methodology explanation, no numbers
    exec_diff_summary: str
    human_factual: int
    human_complete: int
    human_no_halluc: int
    human_failure_category: str | None
    note: str


# Calibration cases for the methodology-grading rubric.
# Each case targets one or more of: factual_accuracy (does methodology match SQL?),
# completeness (does methodology answer the question?), no_hallucination (does it
# stay within the SQL/data?). Numbers in explanations are NOT expected — the
# product surfaces them via the result table.
VALIDATION_CASES: list[ValidationCase] = [
    {
        "id": "v01_clean_aligned",
        "prompt": "How many active members do we have?",
        "peak_sql": "SELECT COUNT(*) AS active_members FROM members WHERE status = 'active';",
        "agent_sql": "SELECT COUNT(*) AS active_members FROM members WHERE status = 'active';",
        "peak_result_csv": "active_members\n5421\n",
        "agent_answer": (
            "Counted rows in the members table where status = 'active' to get the total active member count."
        ),
        "exec_diff_summary": "results match",
        "human_factual": 1,
        "human_complete": 1,
        "human_no_halluc": 1,
        "human_failure_category": None,
        "note": "happy path — methodology faithful to SQL, addresses the question, no extras.",
    },
    {
        "id": "v02_methodology_lies_about_filter",
        "prompt": "How many active members do we have?",
        "peak_sql": "SELECT COUNT(*) AS active_members FROM members WHERE status = 'active';",
        "agent_sql": "SELECT COUNT(*) AS active_members FROM members;",
        "peak_result_csv": "active_members\n5421\n",
        "agent_answer": (
            "Counted rows in the members table where status = 'active' to get the total active member count."
        ),
        "exec_diff_summary": "row count differs: peak=1, agent=1; agent value differs (no filter applied)",
        "human_factual": 0,
        "human_complete": 1,
        "human_no_halluc": 1,
        "human_failure_category": "missing_filter",
        "note": "methodology claims a filter that isn't in the agent's SQL — factual fails.",
    },
    {
        "id": "v03_methodology_describes_wrong_grouping",
        "prompt": "What was the daily ride count last week?",
        "peak_sql": "SELECT ride_date, COUNT(*) AS rides FROM rides WHERE ride_date >= '2026-04-27' GROUP BY ride_date;",
        "agent_sql": "SELECT DATE_TRUNC(ride_date, MONTH) AS ride_date, COUNT(*) AS rides FROM rides WHERE ride_date >= '2026-04-27' GROUP BY 1;",
        "peak_result_csv": "ride_date,rides\n2026-04-27,142\n2026-04-28,156\n",
        "agent_answer": (
            "Aggregated the rides table by day for the last week, counting rows per day."
        ),
        "exec_diff_summary": "row count differs (peak=7, agent=1) — agent grouped by month, not day",
        "human_factual": 0,
        "human_complete": 1,
        "human_no_halluc": 1,
        "human_failure_category": "wrong_grouping",
        "note": "methodology says 'by day' but SQL groups by month — pure grouping bug, same filter and columns as peak.",
    },
    {
        "id": "v04_missing_dimension",
        "prompt": "What's our average ride duration by station and month?",
        "peak_sql": "SELECT month, station, avg_duration_min FROM monthly_station_duration;",
        "agent_sql": "SELECT month, avg_duration_min FROM monthly_station_duration;",
        "peak_result_csv": "month,station,avg_duration_min\n2026-04,central,14.2\n",
        "agent_answer": (
            "Selected month and avg_duration_min from the monthly_station_duration table."
        ),
        "exec_diff_summary": "column mismatch (peak: ['month', 'station', 'avg_duration_min'], agent: ['month', 'avg_duration_min']) — agent omits station",
        "human_factual": 1,
        "human_complete": 0,
        "human_no_halluc": 1,
        "human_failure_category": "wrong_columns",
        "note": "same table as peak, but agent drops the station column the user asked for — pure column-selection bug.",
    },
    {
        "id": "v05_unsupported_trend_claim",
        "prompt": "What's our monthly membership churn rate for the last 6 months?",
        "peak_sql": "SELECT month, churn_rate FROM monthly_churn WHERE month >= '2025-11';",
        "agent_sql": "SELECT month, churn_rate FROM monthly_churn WHERE month >= '2025-11';",
        "peak_result_csv": "month,churn_rate\n2025-11,0.024\n2026-04,0.028\n",
        "agent_answer": (
            "Selected month and churn_rate from monthly_churn for the trailing 6 months. "
            "Churn has been steadily declining over this window, consistent with the new ride-pass features."
        ),
        "exec_diff_summary": "results match",
        "human_factual": 1,
        "human_complete": 1,
        "human_no_halluc": 0,
        "human_failure_category": None,
        "note": "correct methodology + ungrounded trend claim and causal story — no_hallucination fails.",
    },
    {
        "id": "v06_industry_comparison_added",
        "prompt": "What is our overall ride cancellation rate?",
        "peak_sql": "SELECT COUNT(*) FILTER (WHERE status = 'cancelled') * 1.0 / COUNT(*) AS cancellation_rate FROM rides;",
        "agent_sql": "SELECT SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS cancellation_rate FROM rides;",
        "peak_result_csv": "cancellation_rate\n0.0327\n",
        "agent_answer": (
            "Computed the share of rides with status='cancelled' over the total ride count. "
            "This is in line with industry-standard cancellation rates for bike-share systems."
        ),
        "exec_diff_summary": "results match",
        "human_factual": 1,
        "human_complete": 1,
        "human_no_halluc": 0,
        "human_failure_category": None,
        "note": "methodology accurate, but adds an industry comparison not derivable from the data.",
    },
    {
        "id": "v07_describes_wrong_metric",
        "prompt": "What is our overall ride cancellation rate?",
        "peak_sql": "SELECT COUNT(*) FILTER (WHERE status = 'cancelled') * 1.0 / COUNT(*) AS cancellation_rate FROM rides;",
        "agent_sql": "SELECT COUNT(*) FILTER (WHERE status = 'cancelled') AS cancellation_rate FROM rides;",
        "peak_result_csv": "cancellation_rate\n0.0327\n",
        "agent_answer": (
            "Counted rides where status='cancelled' from the rides table."
        ),
        "exec_diff_summary": "values differ (peak=0.0327 rate, agent=164 count) — agent omits divide-by-total",
        "human_factual": 1,
        "human_complete": 0,
        "human_no_halluc": 1,
        "human_failure_category": "wrong_aggregation",
        "note": "methodology faithful to its SQL, but SQL counts cancellations instead of dividing by total — pure aggregation bug, same column.",
    },
    {
        "id": "v08_recommendation_added",
        "prompt": "What is our average rides per month for premium members?",
        "peak_sql": "SELECT AVG(rides_per_month) AS avg_rides FROM members WHERE tier = 'premium';",
        "agent_sql": "SELECT AVG(rides_per_month) AS avg_rides FROM members WHERE tier = 'premium';",
        "peak_result_csv": "avg_rides\n18.0\n",
        "agent_answer": (
            "Averaged rides_per_month across members where tier='premium'. "
            "We should focus retention efforts on premium members to grow this segment."
        ),
        "exec_diff_summary": "results match",
        "human_factual": 1,
        "human_complete": 1,
        "human_no_halluc": 0,
        "human_failure_category": None,
        "note": "methodology correct, but adds a strategic recommendation that's not derivable from the data.",
    },
    {
        "id": "v09_dodge_methodology",
        "prompt": "What's our monthly visitor-to-signup conversion rate?",
        "peak_sql": "SELECT month, signups / visitors AS conversion_rate FROM monthly_conversion;",
        "agent_sql": "SELECT month, signups / visitors AS conversion_rate FROM monthly_conversion;",
        "peak_result_csv": "month,conversion_rate\n2026-04,0.085\n",
        "agent_answer": (
            "I queried the warehouse and computed the answer."
        ),
        "exec_diff_summary": "results match",
        "human_factual": 1,
        "human_complete": 0,
        "human_no_halluc": 1,
        "human_failure_category": None,
        "note": "vague methodology — fails completeness (doesn't describe the computation), but makes no specific false claim about SQL operations, so factual passes under the orthogonal-dimensions rule.",
    },
    {
        "id": "v11_metric_label_not_factual_claim",
        "prompt": "What is the active dock capacity by month?",
        "peak_sql": "SELECT month, SUM(IF(is_operational, dock_capacity, 0)) AS active_capacity FROM monthly_stations GROUP BY month;",
        "agent_sql": "SELECT month, SUM(dock_capacity) AS active_capacity FROM monthly_stations GROUP BY month;",
        "peak_result_csv": "month,active_capacity\n2026-04,86460\n",
        "agent_answer": (
            "Aggregated the active dock capacity from monthly_stations, summed by month."
        ),
        "exec_diff_summary": "values differ in 50 rows (12 mismatched) — agent omits peak's operational-station zeroing",
        "human_factual": 1,
        "human_complete": 1,
        "human_no_halluc": 1,
        "human_failure_category": "missing_filter",
        "note": "metric label 'active' echoes the user's question; methodology faithfully describes its own SQL. SQL bug (missing operational-station filter) belongs in exec_match + missing_filter, not factual_accuracy.",
    },
    {
        "id": "v12_methodology_makes_specific_false_claim",
        "prompt": "What's our repeat-rider rate for returning members, by month?",
        "peak_sql": "SELECT month, SAFE_DIVIDE(COUNT(DISTINCT IF(is_repeat_ride AND is_completed, member_id, NULL)), COUNT(DISTINCT IF(is_completed, member_id, NULL))) AS repeat_rate FROM rides GROUP BY month;",
        "agent_sql": "SELECT month, SAFE_DIVIDE(COUNT(DISTINCT IF(NOT is_first_ride AND is_completed, member_id, NULL)), COUNT(DISTINCT IF(has_membership AND NOT is_first_ride, member_id, NULL))) AS repeat_rate FROM rides GROUP BY month;",
        "peak_result_csv": "month,repeat_rate\n2026-04,0.085\n",
        "agent_answer": (
            "Identified returning members using NOT is_first_ride, then computed repeat rate as repeat rides divided by returning-member rides by month."
        ),
        "exec_diff_summary": "results match",
        "human_factual": 0,
        "human_complete": 1,
        "human_no_halluc": 1,
        "human_failure_category": None,
        "note": "methodology makes a specific claim ('using NOT is_first_ride') that doesn't match agent's SQL — denominator uses has_membership AND NOT is_first_ride, numerator uses is_completed AND NOT is_first_ride. Real factual lie about SQL operations, not just a domain word.",
    },
    {
        "id": "v10_methodology_lies_about_aggregation",
        "prompt": "What is the total number of rides last month?",
        "peak_sql": "SELECT COUNT(*) AS total_rides FROM rides WHERE ride_month = '2026-04';",
        "agent_sql": "SELECT SUM(distance_km) AS total_rides FROM rides WHERE ride_month = '2026-04';",
        "peak_result_csv": "total_rides\n8421\n",
        "agent_answer": (
            "Counted the rows in the rides table for last month to get the total ride count."
        ),
        "exec_diff_summary": "values differ (peak counts rows, agent sums distance_km column)",
        "human_factual": 0,
        "human_complete": 1,
        "human_no_halluc": 1,
        "human_failure_category": "wrong_aggregation",
        "note": "methodology says COUNT, but agent SQL uses SUM(distance_km) — wrong aggregation described.",
    },
]


def _read_csv(text: str) -> pd.DataFrame:
    return pd.read_csv(StringIO(text))


def main() -> int:
    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY not set (copy .env.example to .env)")
        return 1

    client = Anthropic()
    reset_usage()

    total = len(VALIDATION_CASES)
    print(f"Calibrating judge against {total} hand-scored cases...\n", flush=True)

    judged: list[dict] = []
    for i, case in enumerate(VALIDATION_CASES, 1):
        prefix = f"  [{i}/{total}] {case['id']}"
        df = _read_csv(case["peak_result_csv"])
        with spinner(f"{prefix} → judging"):
            result = llm_judge(
                prompt=case["prompt"],
                agent_answer=case["agent_answer"],
                peak_result_df=df,
                peak_sql=case["peak_sql"],
                agent_sql=case["agent_sql"],
                exec_diff_summary=case["exec_diff_summary"],
                client=client,
            )
        human = (case["human_factual"], case["human_complete"], case["human_no_halluc"])
        scores = (
            result["factual_accuracy"],
            result["completeness"],
            result["no_hallucination"],
        )
        cat_human = case["human_failure_category"]
        cat_judge = result.get("failure_category")
        cat_agreed = cat_human == cat_judge
        agreed = sum(1 for h, j in zip(human, scores) if h == j) + (
            1 if cat_agreed else 0
        )
        print(
            f"{prefix}  human={human} judge={scores} cat={cat_human}→{cat_judge} agreed={agreed}/4",
            flush=True,
        )
        judged.append(
            {
                "id": case["id"],
                "human": human,
                "judge": scores,
                "human_category": cat_human,
                "judge_category": cat_judge,
                "category_agreed": cat_agreed,
                "reasoning": result["reasoning"],
                "note": case["note"],
            }
        )

    n = len(judged)
    agreements = {dim: 0 for dim in DIMENSIONS}
    kappas = {dim: 0.0 for dim in DIMENSIONS}
    for idx, dim in enumerate(DIMENSIONS):
        humans = [row["human"][idx] for row in judged]
        judges = [row["judge"][idx] for row in judged]
        agreements[dim] = sum(1 for h, j in zip(humans, judges) if h == j)
        kappas[dim] = cohens_kappa(humans, judges)
    category_agreed = sum(1 for row in judged if row["category_agreed"])

    summary = []
    for dim in DIMENSIONS:
        rate = agreements[dim] / n
        k = kappas[dim]
        rate_warn = " ⚠" if rate < AGREEMENT_THRESHOLD else ""
        k_warn = " ⚠" if k < KAPPA_SUBSTANTIAL else ""
        summary.append([
            dim,
            f"{agreements[dim]}/{n}",
            f"{rate:.0%}{rate_warn}",
            f"{k:+.2f}{k_warn}",
            kappa_label(k),
        ])
    cat_rate = category_agreed / n
    cat_warn = " ⚠" if cat_rate < AGREEMENT_THRESHOLD else ""
    summary.append(
        ["failure_category", f"{category_agreed}/{n}", f"{cat_rate:.0%}{cat_warn}", "—", "n/a (multi-class)"]
    )
    summary_table = tabulate(
        summary,
        headers=["dimension", "agreed", "rate", "Cohen's κ", "Landis-Koch"],
        tablefmt="github",
    )
    print(f"\n=== Per-dimension agreement (n={n}) ===")
    print(summary_table)

    total_agreements = sum(agreements.values()) + category_agreed
    overall = total_agreements / (n * (len(DIMENSIONS) + 1))
    print(
        f"\nOverall agreement: {overall:.0%} "
        f"({total_agreements}/{n * (len(DIMENSIONS) + 1)})"
    )

    disagreements = []
    for row in judged:
        for idx, dim in enumerate(DIMENSIONS):
            if row["human"][idx] != row["judge"][idx]:
                disagreements.append(
                    [
                        row["id"],
                        dim,
                        row["human"][idx],
                        row["judge"][idx],
                        (row["reasoning"][:90] + "...")
                        if len(row["reasoning"]) > 90
                        else row["reasoning"],
                    ]
                )

    if disagreements:
        disagreements_table = tabulate(
            disagreements,
            headers=["case", "dimension", "human", "judge", "judge reasoning"],
            tablefmt="github",
        )
        print("\n=== Disagreements ===")
        print(disagreements_table)
    else:
        disagreements_table = "(none — full alignment with human scores)"
        print("\nNo disagreements — full alignment with human scores.")

    below_agreement = [dim for dim in DIMENSIONS if agreements[dim] / n < AGREEMENT_THRESHOLD]
    below_kappa = [dim for dim in DIMENSIONS if kappas[dim] < KAPPA_SUBSTANTIAL]
    if below_agreement:
        print(
            f"\n⚠ {', '.join(below_agreement)} below {AGREEMENT_THRESHOLD:.0%} raw agreement."
        )
    if below_kappa:
        print(
            f"⚠ {', '.join(below_kappa)} below κ={KAPPA_SUBSTANTIAL:.2f} (Landis-Koch substantial) — "
            f"this is the load-bearing signal; raw agreement can be inflated by class skew."
        )
    if below_agreement or below_kappa:
        print("→ Iterate JUDGE_PROMPT in judge.py and re-run.")
    else:
        print(f"\n✓ All dimensions clear both bars (≥{AGREEMENT_THRESHOLD:.0%} agreement AND κ≥{KAPPA_SUBSTANTIAL:.2f}).")

    usage = get_usage()
    print(f"\nUsage: {format_usage(usage)}")

    file_ts, iso_ts = utc_timestamp()
    body = _render_calibration_report(
        judged=judged,
        agreements=agreements,
        overall=overall,
        summary_table=summary_table,
        disagreements_table=disagreements_table,
        below=below,
        iso_ts=iso_ts,
        usage_str=format_usage(usage),
    )
    out_path = CALIBRATION_DIR / f"{file_ts}_{judge_prompt_hash()}.md"
    write_run(out_path, body)
    rel = out_path.relative_to(Path.cwd()) if out_path.is_relative_to(Path.cwd()) else out_path
    print(f"\nReport saved to {rel}")
    return 0


def _render_calibration_report(
    *,
    judged: list[dict],
    agreements: dict,
    overall: float,
    summary_table: str,
    disagreements_table: str,
    below: list[str],
    iso_ts: str,
    usage_str: str,
) -> str:
    n = len(judged)
    lines = [
        f"# peak judge calibration run — {iso_ts}",
        "",
        f"- **Judge model:** {JUDGE_MODEL}",
        f"- **JUDGE_PROMPT hash:** `{judge_prompt_hash()}`",
        f"- **Cases:** {n}",
        f"- **Overall agreement:** {overall:.0%} "
        f"({sum(agreements.values())}/{n * len(DIMENSIONS)})",
        f"- **Usage:** {usage_str}",
    ]
    if below:
        lines.append(
            f"- **⚠ Below {AGREEMENT_THRESHOLD:.0%} threshold:** {', '.join(below)}"
        )
    lines += [
        "",
        "## Per-dimension agreement",
        "",
        summary_table,
        "",
        "## Disagreements",
        "",
        disagreements_table,
        "",
        "## All cases",
        "",
    ]
    rows = [
        [
            r["id"],
            f"({','.join(str(x) for x in r['human'])})",
            f"({','.join(str(x) for x in r['judge'])})",
            sum(1 for h, j in zip(r["human"], r["judge"]) if h == j),
            r["note"],
        ]
        for r in judged
    ]
    lines.append(
        tabulate(
            rows,
            headers=["case", "human", "judge", "agreed/3", "note"],
            tablefmt="github",
        )
    )
    lines += ["", "## Per-case judge reasoning", ""]
    for r in judged:
        lines.append(f"### {r['id']}")
        lines.append("")
        lines.append(f"- **Human:** {r['human']}")
        lines.append(f"- **Judge:** {r['judge']}")
        lines.append(f"- **Reasoning:** {r['reasoning']}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
