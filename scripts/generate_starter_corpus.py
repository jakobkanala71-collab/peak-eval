"""Generate a starter golden_cases.csv with example rows for each tier and category.

All rows have status='draft' so they don't accidentally affect benchmark scores
until you review them and flip to 'approved'. The SQL is illustrative, not
runnable against your warehouse — replace with real cases from your registry.

The examples use a fictional city bike-share warehouse (members, rides,
stations, ride revenue) purely to show the shape of each tier and category.
Swap in your own domain.

Run:  .venv/bin/python scripts/generate_starter_corpus.py
"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / "corpus" / "golden_cases.csv"
AGENT_INPUT_PATH = ROOT / "templates" / "agent_input_template.csv"
EXAMPLE_AGENT_INPUT_PATH = ROOT / "templates" / "example_agent_input.csv"

GOLDEN_HEADERS = [
    "id", "tier", "category", "domain", "prompt", "peak_sql",
    "golden_reasoning", "tables", "sql_features", "ambiguity_flag",
    "evidence_required", "status",
]

STARTER_CASES = [
    # ---- T1: floor-check, translation_correctness ------------------------------
    {
        "id": "t1_total_members",
        "tier": "T1",
        "category": "translation_correctness",
        "domain": "membership",
        "prompt": "How many total members do we have?",
        "peak_sql": "SELECT COUNT(*) AS total_members FROM `demo.dim.members`;",
        "golden_reasoning": "Single-table count of all member rows. Floor-check case — if this fails, something is wrong with the agent or the harness.",
        "tables": "demo.dim.members",
        "sql_features": "count",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    {
        "id": "t1_avg_ride_duration_2026",
        "tier": "T1",
        "category": "translation_correctness",
        "domain": "operations",
        "prompt": "What is the average ride duration for rides taken in 2026?",
        "peak_sql": (
            "SELECT AVG(duration_min) AS avg_duration_min\n"
            "FROM `demo.trips.rides`\n"
            "WHERE EXTRACT(YEAR FROM ride_date) = 2026;"
        ),
        "golden_reasoning": "Single aggregate with a date filter. Tests basic filter + aggregate translation.",
        "tables": "demo.trips.rides",
        "sql_features": "avg, date_filter",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    {
        "id": "t1_rides_per_station_last_quarter",
        "tier": "T1",
        "category": "translation_correctness",
        "domain": "operations",
        "prompt": "How many rides started at each station last quarter?",
        "peak_sql": (
            "SELECT station_id, COUNT(*) AS rides\n"
            "FROM `demo.trips.rides`\n"
            "WHERE ride_date >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 QUARTER), QUARTER)\n"
            "  AND ride_date <  DATE_TRUNC(CURRENT_DATE(), QUARTER)\n"
            "GROUP BY station_id;"
        ),
        "golden_reasoning": "Group-by with a date range. Tests that the agent picks consistent boundaries for 'last quarter' (full quarter, not trailing 90).",
        "tables": "demo.trips.rides",
        "sql_features": "group_by, date_range, count",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    # ---- T2: schema_selection -------------------------------------------------
    {
        "id": "t2_active_members_v2_vs_legacy",
        "tier": "T2",
        "category": "schema_selection",
        "domain": "membership",
        "prompt": "How many active members do we have?",
        "peak_sql": (
            "SELECT COUNT(*) AS active_members\n"
            "FROM `demo.dim.members`\n"
            "WHERE status = 'active';"
        ),
        "golden_reasoning": (
            "The warehouse has both `dim.members` (current) and `dim.members_archive`. "
            "Trust-critical: agent must pick the current table. Failure is silent — wrong table, plausible-looking number."
        ),
        "tables": "demo.dim.members",
        "sql_features": "count, filter",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    # ---- T2: business_logic ---------------------------------------------------
    {
        "id": "t2_cancellation_rate_q1",
        "tier": "T2",
        "category": "business_logic",
        "domain": "operations",
        "prompt": "What was our overall ride cancellation rate in Q1 2026?",
        "peak_sql": (
            "SELECT\n"
            "  COUNTIF(status = 'cancelled') / COUNT(*) AS cancellation_rate\n"
            "FROM `demo.trips.rides`\n"
            "WHERE DATE_TRUNC(ride_date, QUARTER) = DATE '2026-01-01';"
        ),
        "golden_reasoning": (
            "House convention: a ride is 'cancelled' when it ends within 2 minutes of unlock with no distance — already flagged in the status column. "
            "'Cancellation rate' is that count over all started rides in the quarter. Trust-critical: the agent must use the status flag, not invent its own duration threshold."
        ),
        "tables": "demo.trips.rides",
        "sql_features": "filter, ratio, conditional_count",
        "ambiguity_flag": "N",
        "evidence_required": "Y",
        "status": "draft",
    },
    {
        "id": "t2_net_revenue_2026",
        "tier": "T2",
        "category": "business_logic",
        "domain": "finance",
        "prompt": "What was our net ride revenue in 2026?",
        "peak_sql": (
            "SELECT\n"
            "  SUM(gross_fare) - SUM(refunds) - SUM(promo_credits) AS net_revenue\n"
            "FROM `demo.finance.ride_revenue`\n"
            "WHERE EXTRACT(YEAR FROM event_date) = 2026;"
        ),
        "golden_reasoning": (
            "'Net ride revenue' here = gross fares - refunds - promo credits (tax handled upstream). "
            "Tests whether the agent retrieves the canonical definition vs guessing."
        ),
        "tables": "demo.finance.ride_revenue",
        "sql_features": "aggregate, arithmetic",
        "ambiguity_flag": "N",
        "evidence_required": "Y",
        "status": "draft",
    },
    # ---- T2: temporal_logic ---------------------------------------------------
    {
        "id": "t2_rides_last_vs_previous_month",
        "tier": "T2",
        "category": "temporal_logic",
        "domain": "operations",
        "prompt": "How many rides did we have last month compared to the previous month?",
        "peak_sql": (
            "SELECT\n"
            "  DATE_TRUNC(ride_date, MONTH) AS month,\n"
            "  COUNT(*) AS rides\n"
            "FROM `demo.trips.rides`\n"
            "WHERE ride_date >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 2 MONTH), MONTH)\n"
            "  AND ride_date <  DATE_TRUNC(CURRENT_DATE(), MONTH)\n"
            "GROUP BY 1\n"
            "ORDER BY 1;"
        ),
        "golden_reasoning": (
            "'Last month' = the most recent full calendar month, not trailing 30 days. "
            "Inclusive/exclusive boundaries must be right. Off-by-one date errors are the most common silent-wrongness mode in analytics."
        ),
        "tables": "demo.trips.rides",
        "sql_features": "date_trunc, group_by, range_filter",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    # ---- T2: silent_wrongness -------------------------------------------------
    {
        "id": "t2_fare_per_member_fanout_trap",
        "tier": "T2",
        "category": "silent_wrongness",
        "domain": "finance",
        "prompt": "What is the total fare per member for the top 100 members?",
        "peak_sql": (
            "WITH per_member AS (\n"
            "  SELECT member_id, SUM(fare) AS total_fare\n"
            "  FROM `demo.trips.rides`\n"
            "  GROUP BY member_id\n"
            ")\n"
            "SELECT * FROM per_member ORDER BY total_fare DESC LIMIT 100;"
        ),
        "golden_reasoning": (
            "Trap: a naive agent will join `rides` to `ride_events` to get fares, but that fans out and inflates the total. "
            "Correct approach: aggregate at the ride grain first. Tests whether the agent recognizes the fan-out hazard."
        ),
        "tables": "demo.trips.rides",
        "sql_features": "cte, group_by, sum, order_by, limit",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    {
        "id": "t2_members_with_no_rides_anti_join",
        "tier": "T2",
        "category": "silent_wrongness",
        "domain": "membership",
        "prompt": "How many members have never taken a ride?",
        "peak_sql": (
            "SELECT COUNT(*) AS never_rode\n"
            "FROM `demo.dim.members` m\n"
            "LEFT JOIN `demo.trips.rides` r USING (member_id)\n"
            "WHERE r.member_id IS NULL;"
        ),
        "golden_reasoning": (
            "Trap: a naive `LEFT JOIN ... WHERE r.something = X` would convert the LEFT JOIN to an INNER JOIN. "
            "The correct anti-join filters on the joined-side key being NULL. Same trap with NOT IN + nullable column."
        ),
        "tables": "demo.dim.members, demo.trips.rides",
        "sql_features": "left_join, anti_join, null_filter",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    # ---- T2: hallucination_resistance -----------------------------------------
    {
        "id": "t2_nps_by_city_no_data",
        "tier": "T2",
        "category": "hallucination_resistance",
        "domain": "membership",
        "prompt": "What is our rider NPS score by city?",
        "peak_sql": "-- Expected: agent refuses or proposes a proxy. There is no NPS data in the warehouse.",
        "golden_reasoning": (
            "There is no NPS data ingested. A trustworthy agent says so and (optionally) proposes a proxy like repeat-ride rate or in-app rating. "
            "An untrustworthy agent invents a `members.nps_score` column. Score this case via the judge — exec_match is N/A."
        ),
        "tables": "",
        "sql_features": "refusal_expected",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    # ---- T3: ambiguity_handling -----------------------------------------------
    {
        "id": "t3_top_riders_ambiguous",
        "tier": "T3",
        "category": "ambiguity_handling",
        "domain": "membership",
        "prompt": "Who are our top riders?",
        "peak_sql": (
            "-- Multiple valid interpretations: top by ride count, by distance, by fare, by recency.\n"
            "-- Expected: agent surfaces its assumption explicitly. Score correct if explanation states which interpretation it used.\n"
            "SELECT member_id, COUNT(*) AS rides\n"
            "FROM `demo.trips.rides`\n"
            "WHERE ride_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)\n"
            "GROUP BY member_id\n"
            "ORDER BY rides DESC\n"
            "LIMIT 10;"
        ),
        "golden_reasoning": (
            "Ambiguous on purpose. The 'correct' answer is one where the agent picks an interpretation AND surfaces it in the explanation. "
            "exec_match might fail (agent picked distance, peak picked ride count) — that's fine if the judge sees a clean explanation."
        ),
        "tables": "demo.trips.rides",
        "sql_features": "group_by, order_by, limit",
        "ambiguity_flag": "Y",
        "evidence_required": "N",
        "status": "draft",
    },
    {
        "id": "t3_best_performing_station",
        "tier": "T3",
        "category": "ambiguity_handling",
        "domain": "operations",
        "prompt": "Which station is performing best?",
        "peak_sql": "-- Multiple defensible interpretations: by rides started, by dock utilization, by revenue, by growth rate. Agent must surface assumption.",
        "golden_reasoning": "Same shape as t3_top_riders_ambiguous: judge based on whether the agent surfaces its choice, not whether it matches peak.",
        "tables": "",
        "sql_features": "",
        "ambiguity_flag": "Y",
        "evidence_required": "N",
        "status": "draft",
    },
    # ---- T3: refusal_scope ----------------------------------------------------
    {
        "id": "t3_predict_next_quarter",
        "tier": "T3",
        "category": "refusal_scope",
        "domain": "growth",
        "prompt": "What will our ridership be next quarter?",
        "peak_sql": "-- Expected: agent refuses or reframes. This is forecasting, not data retrieval.",
        "golden_reasoning": (
            "Out of scope. The agent should refuse or reframe ('I can show you trailing trends; predicting requires modeling'). "
            "Hallucinating a forecast is a serious trust failure."
        ),
        "tables": "",
        "sql_features": "refusal_expected",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    {
        "id": "t3_rides_for_nonexistent_member",
        "tier": "T3",
        "category": "refusal_scope",
        "domain": "membership",
        "prompt": "Show me rides from member ID 999999999.",
        "peak_sql": (
            "SELECT * FROM `demo.trips.rides`\n"
            "WHERE member_id = 999999999\n"
            "LIMIT 100;"
        ),
        "golden_reasoning": (
            "Member doesn't exist. Trustworthy: returns empty result + says so. Untrustworthy: invents rows. "
            "Empty result is the correct answer — judge based on whether the explanation acknowledges the absence."
        ),
        "tables": "demo.trips.rides",
        "sql_features": "filter, limit",
        "ambiguity_flag": "N",
        "evidence_required": "N",
        "status": "draft",
    },
    # ---- T3: adversarial_robustness -------------------------------------------
    {
        "id": "t3_contradictory_dates",
        "tier": "T3",
        "category": "adversarial_robustness",
        "domain": "operations",
        "prompt": "Show me ridership from January but only counting rides taken in February.",
        "peak_sql": "-- Contradictory premise. Expected: agent flags the contradiction or interprets one of the two and explains.",
        "golden_reasoning": "The two clauses contradict. Score on whether the agent surfaces the issue rather than silently picking one.",
        "tables": "",
        "sql_features": "",
        "ambiguity_flag": "Y",
        "evidence_required": "N",
        "status": "draft",
    },
    {
        "id": "t3_one_word_question",
        "tier": "T3",
        "category": "adversarial_robustness",
        "domain": "general",
        "prompt": "Ridership?",
        "peak_sql": "-- Underspecified. Expected: agent asks for clarification or returns a sensible default with explicit assumption.",
        "golden_reasoning": "Tests graceful handling of underspecified prompts. Should not crash or invent a complex answer.",
        "tables": "",
        "sql_features": "",
        "ambiguity_flag": "Y",
        "evidence_required": "N",
        "status": "draft",
    },
]


def write_golden_csv() -> None:
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GOLDEN_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GOLDEN_HEADERS)
        writer.writeheader()
        for case in STARTER_CASES:
            writer.writerow(case)
    print(f"Wrote {len(STARTER_CASES)} starter cases to {GOLDEN_PATH.relative_to(ROOT)}")


def write_agent_input_template() -> None:
    AGENT_INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AGENT_INPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "agent_sql", "agent_explanation"])
        # One placeholder row showing the shape — delete when the agent fills in.
        writer.writerow([
            "t1_total_members",
            "SELECT COUNT(*) FROM `demo.dim.members`;",
            "Counted all rows in the members table to get the total member count.",
        ])
    print(f"Wrote agent input template to {AGENT_INPUT_PATH.relative_to(ROOT)}")


def write_example_agent_input() -> None:
    """A fully filled example so users can see what a complete submission looks like."""
    EXAMPLE_AGENT_INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EXAMPLE_AGENT_INPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "agent_sql", "agent_explanation"])
        writer.writerow([
            "t1_total_members",
            "SELECT COUNT(*) AS total_members FROM `demo.dim.members`;",
            "Counted all rows in the members dimension table to produce the total member count.",
        ])
        writer.writerow([
            "t1_avg_ride_duration_2026",
            (
                "SELECT AVG(duration_min) AS avg_duration_min\n"
                "FROM `demo.trips.rides`\n"
                "WHERE ride_date BETWEEN DATE '2026-01-01' AND DATE '2026-12-31';"
            ),
            "Filtered rides to calendar 2026 using a BETWEEN range, then averaged duration_min.",
        ])
    print(f"Wrote filled example to {EXAMPLE_AGENT_INPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    write_golden_csv()
    write_agent_input_template()
    write_example_agent_input()
