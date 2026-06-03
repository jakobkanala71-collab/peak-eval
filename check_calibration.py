"""Print judge calibration freshness without running anything.

No API calls, no BigQuery — just reads runs/calibration/ and compares the
latest file's prompt hash to the current JUDGE_PROMPT hash.

Run: .venv/bin/python check_calibration.py
"""

from __future__ import annotations

from pathlib import Path

from judge import judge_prompt_hash
from report import format_calibration_status, latest_calibration

CALIBRATION_DIR = Path(__file__).resolve().parent / "runs" / "calibration"


def main() -> int:
    status = latest_calibration(CALIBRATION_DIR)
    current = judge_prompt_hash()
    print(f"Current JUDGE_PROMPT hash: {current}")
    print(format_calibration_status(current, status))
    if status:
        print(f"Calibration file: {status.path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
