"""Generate reference CSVs that go alongside golden_cases.csv as Sheet tabs.

These CSVs are *reference material* for corpus authors — they don't drive the
benchmark. Import each as a separate tab into the corpus Google Sheet so
analysts can see categories, tiers, and instructions in one place.

Run:  .venv/bin/python scripts/generate_reference_csvs.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from categories import CATEGORIES  # noqa: E402

TABS_DIR = ROOT / "templates" / "sheet_tabs"


CATEGORY_DESCRIPTIONS = {
    "translation_correctness": "Baseline competency: aggregates, joins, window functions, CTEs. Floor check that the agent can produce executable SQL for clearly-specified questions.",
    "schema_selection": "Picks the right table when several look plausible (rides vs rides_v2, members vs members_archive). Failures = confident-looking wrong numbers.",
    "business_logic": "Knows domain conventions without being told (net revenue, cancelled ride, active member). Failures = wrong number with the right shape.",
    "temporal_logic": "Date math, time zones, fiscal calendars, period comparisons. Off-by-one errors are the #1 silent-wrongness mode.",
    "silent_wrongness": "Cases designed to produce wrong-but-plausible answers under naive SQL: fan-out joins, NULL semantics, mixed grain, type coercion.",
    "ambiguity_handling": "Question has multiple valid interpretations. Tests whether the agent surfaces its assumption or silently picks one.",
    "hallucination_resistance": "Asks for data the warehouse doesn't have. Tests whether the agent refuses or fabricates a column.",
    "refusal_scope": "Out-of-scope questions (forecasting, real-time data, false premises). Tests whether the agent recognizes the gap.",
    "efficiency": "Cheap vs expensive correct SQL. Measured on bytes scanned ratio across all correct cases.",
    "reasoning_faithfulness": "Does the explanation accurately describe what the SQL did? Caught by the LLM judge across all cases.",
    "consistency_rephrasing": "Same question, different phrasings — does the agent give the same answer? Measured by including phrasing-variant cases.",
    "adversarial_robustness": "Malformed, contradictory, or partial questions. Tests whether the agent fails gracefully.",
}

CATEGORY_EXAMPLE = {
    "translation_correctness": "How many members do we have?",
    "schema_selection": "Active members — when there's members, members_v2, members_archive.",
    "business_logic": "Cancellation rate in Q1 — house counts a ride cancelled if it ends < 2 min with no distance.",
    "temporal_logic": "Rides last month vs the previous month.",
    "silent_wrongness": "Total fare per member — fan-out trap if joined through ride_events.",
    "ambiguity_handling": "Top riders — by what metric?",
    "hallucination_resistance": "Rider NPS by city — there is no NPS data.",
    "refusal_scope": "What will ridership be next quarter? — out of scope.",
    "efficiency": "(measured on all correct cases — no dedicated questions)",
    "reasoning_faithfulness": "(measured on all judged cases — no dedicated questions)",
    "consistency_rephrasing": "Multiple phrasings of the same question (overlay).",
    "adversarial_robustness": "Show me ridership from January but only February rides.",
}


def write_categories_tab() -> None:
    path = TABS_DIR / "categories.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["slug", "title", "trust_risk", "trust_critical", "calibration",
                    "description", "example"])
        for c in CATEGORIES:
            w.writerow([
                c.slug, c.title, c.trust_risk,
                "Y" if c.trust_critical else "N",
                "Y" if c.calibration else "N",
                CATEGORY_DESCRIPTIONS[c.slug],
                CATEGORY_EXAMPLE[c.slug],
            ])
    print(f"Wrote {path.relative_to(ROOT)}")


def write_tiers_tab() -> None:
    path = TABS_DIR / "tiers.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tier", "name", "definition", "scoring_rule", "target_share_of_corpus"])
        w.writerow([
            "T1", "Floor check",
            "Trivial / impossible to fail. Sanity check that the harness and agent both work.",
            "Aggregated as 'Headline correctness' — threshold 90%.",
            "20%",
        ])
        w.writerow([
            "T2", "Main signal",
            "Unambiguous but hard. Real analytical questions with exactly one correct interpretation. Where you actually compare agents.",
            "Trust-critical aggregate (when category is trust-critical). Threshold 85%.",
            "65%",
        ])
        w.writerow([
            "T3", "Judgment calls",
            "Ambiguous by design — multiple valid interpretations. Tests whether the agent recognizes ambiguity.",
            "Calibration aggregate (ambiguity_handling, refusal_scope). Threshold 80%. Judged on explanation, not exec_match.",
            "15%",
        ])
    print(f"Wrote {path.relative_to(ROOT)}")


def write_instructions_tab() -> None:
    path = TABS_DIR / "instructions.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "content"])
        rows = [
            ("Purpose",
             "This Sheet is the source of truth for peak_v2 golden cases. "
             "Edit the 'golden_cases' tab; download as CSV; drop into peak_v2/corpus/golden_cases.csv."),
            ("How to author a case",
             "1. Pick a tier (T1/T2/T3) and a category (one of 12). 2. Write the natural-language prompt. "
             "3. Write the canonical (golden) SQL. 4. Write golden_reasoning explaining what makes it correct. "
             "5. Mark ambiguity_flag (Y if the question has multiple valid readings). "
             "6. Set status='draft' until reviewed; flip to 'approved' when ready to score against."),
            ("Tiers",
             "T1 = trivial (floor check). T2 = unambiguous but hard (main signal). T3 = ambiguous (judgment calls). "
             "See the 'tiers' tab for definitions and scoring rules."),
            ("Categories",
             "12 categories grouped by trust risk. Trust-critical categories are weighted heavily in the scorecard. "
             "See the 'categories' tab for descriptions and examples."),
            ("Status workflow",
             "draft → approved. Only 'approved' rows are loaded by run_benchmark.py (use --include-drafts to override). "
             "Treat 'approved' as a sign-off: this is what we score the agent against."),
            ("Authoring tips",
             "Write prompts as a real analyst would type them — not as SQL pseudo-text. "
             "Each case should have ONE canonical answer (or be flagged ambiguity_flag=Y for T3). "
             "Use the query registry as a candidate pool, but rewrite descriptions into natural prompts."),
            ("Where peak_v2 reads from",
             "peak_v2/corpus/golden_cases.csv. The CSV is the runtime source. The Sheet is the editing surface. "
             "Commit the CSV to git so benchmark runs can reference a corpus version."),
        ]
        for section, content in rows:
            w.writerow([section, content])
    print(f"Wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    write_categories_tab()
    write_tiers_tab()
    write_instructions_tab()
