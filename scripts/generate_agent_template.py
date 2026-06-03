"""Generate templates/agent_input_template.csv from corpus/golden_cases.csv.

The template gets one row per approved golden case, with the prompt included
as context (the loader ignores extra columns) so the agent owner knows what
question each id is asking about without flipping back to the corpus.

agent_sql and agent_explanation are left blank for the agent owner to fill.

Run:  .venv/bin/python scripts/generate_agent_template.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_CSV = ROOT / "corpus" / "golden_cases.csv"
TEMPLATE_CSV = ROOT / "templates" / "agent_input_template.csv"


def main() -> int:
    if not GOLDEN_CSV.exists():
        print(f"❌ {GOLDEN_CSV} not found")
        return 1

    with GOLDEN_CSV.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("status") or "").strip() == "approved"]

    if not rows:
        print(f"❌ no approved rows in {GOLDEN_CSV}")
        return 1

    TEMPLATE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with TEMPLATE_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["id", "prompt", "agent_sql", "agent_explanation"],
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "id": (r.get("id") or "").strip(),
                    "prompt": (r.get("prompt") or "").strip(),
                    "agent_sql": "",
                    "agent_explanation": "",
                }
            )

    print(f"Wrote {len(rows)} rows → {TEMPLATE_CSV.relative_to(ROOT)}")
    print("\nNext: copy to inputs/<agent>.csv and fill in agent_sql + agent_explanation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
