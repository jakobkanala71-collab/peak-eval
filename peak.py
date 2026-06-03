"""peak_v2 — interactive menu.

Single entry point that wraps the individual scripts (benchmark, judge
calibration, report viewer, template generators) behind an arrow-key TUI.

Run:  .venv/bin/python peak.py
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import questionary
    from questionary import Style
except ImportError:
    print("❌ questionary is not installed. Run: .venv/bin/pip install -r requirements.txt")
    sys.exit(1)

import input_fingerprint as fp
from judge import judge_prompt_hash

MENU_STYLE = Style(
    [
        ("qmark", "fg:#5fafff bold"),
        ("question", "bold"),
        ("pointer", "fg:#ff5fd7 bold"),
        ("highlighted", "fg:#ff5fd7 bold"),
        ("selected", "fg:#5fff87 bold"),
        ("answer", "fg:#5fff87 bold"),
    ]
)
POINTER = "▶"
QMARK = "›"


def _term_cols() -> int:
    return shutil.get_terminal_size((80, 20)).columns


def _hr(char: str = "━", color: str = "\x1b[38;5;240m") -> None:
    print(f"{color}{char * _term_cols()}\x1b[0m")


def _header(title: str) -> None:
    print()
    _hr()
    print(f"  \x1b[1;96m▶ {title}\x1b[0m")
    _hr()
    print()


def _footer() -> None:
    print()
    _hr("─", "\x1b[38;5;240m")
    try:
        input("  \x1b[38;5;245mPress Enter to return to menu…\x1b[0m ")
    except (EOFError, KeyboardInterrupt):
        print()
    print()

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
INPUTS_DIR = ROOT / "inputs"
GOLDEN_CSV = ROOT / "corpus" / "golden_cases.csv"
BENCHMARK_DIR = ROOT / "runs" / "benchmark"
CALIBRATION_DIR = ROOT / "runs" / "calibration"


def _select(question: str, **kwargs):
    kwargs.setdefault("pointer", POINTER)
    kwargs.setdefault("qmark", QMARK)
    kwargs.setdefault("style", MENU_STYLE)
    return questionary.select(question, **kwargs)


# Sentinel for "← back" rows. questionary.Choice falls back to the title
# string when value=None, so an explicit sentinel is needed to disambiguate.
_BACK = "__back__"


def _run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}\n")
    return subprocess.call(cmd, cwd=str(ROOT))


def _pick_agent_csv() -> Path | None:
    csvs = sorted(p for p in INPUTS_DIR.glob("*.csv"))
    if not csvs:
        questionary.print(f"No CSVs found in {INPUTS_DIR}.", style="bold fg:red")
        return None
    choice = _select(
        "Pick an agent input CSV:",
        choices=[questionary.Choice(p.name, value=p) for p in csvs]
        + [questionary.Choice("← back", value=_BACK)],
    ).ask()
    if choice is None or choice == _BACK:
        return None
    return choice


def run_benchmark() -> None:
    agent_csv = _pick_agent_csv()
    if agent_csv is None:
        return

    default_name = agent_csv.stem.replace("_input", "").replace("example_", "")
    agent_name = questionary.text(
        "Agent name (used in the report filename):",
        default=default_name,
    ).ask()
    if not agent_name:
        return

    include_drafts = questionary.confirm(
        "Also run unreviewed (status='draft') cases? Default no — only run cases marked status='approved'.",
        default=False,
    ).ask()

    fingerprint = fp.compute_fingerprint(
        agent_csv,
        judge_prompt_hash=judge_prompt_hash(),
        corpus_csv=GOLDEN_CSV,
    )
    prior = fp.lookup(BENCHMARK_DIR, fingerprint)
    force = False
    if prior:
        questionary.print(
            f"\n⚠ This input was already benchmarked as '{prior.agent_name}' on {prior.timestamp}.",
            style="bold fg:yellow",
        )
        questionary.print(f"  Prior report: {prior.report_path}")
        questionary.print(f"  Prior source: {prior.source_csv}\n")
        choice = _select(
            "What now?",
            choices=[
                "Cancel (default)",
                "Open the prior report",
                "Re-run anyway (--force)",
            ],
            default="Cancel (default)",
        ).ask()
        if choice == "Open the prior report":
            _open_file(ROOT / prior.report_path)
            return
        if choice != "Re-run anyway (--force)":
            return
        force = True

    cmd = [
        PYTHON,
        "run_benchmark.py",
        str(GOLDEN_CSV),
        str(agent_csv),
        "--agent-name",
        agent_name,
    ]
    if include_drafts:
        cmd.append("--include-drafts")
    if force:
        cmd.append("--force")
    _run(cmd)


def validate_judge() -> None:
    _run([PYTHON, "validate_judge.py"])


def check_calibration() -> None:
    _run([PYTHON, "check_calibration.py"])


def _open_file(path: Path) -> None:
    if not path.exists():
        questionary.print(f"❌ {path} not found", style="bold fg:red")
        return
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.call(["open", str(path)])
        elif system == "Linux":
            subprocess.call(["xdg-open", str(path)])
        elif system == "Windows":
            subprocess.call(["cmd", "/c", "start", "", str(path)], shell=False)
        else:
            questionary.print(f"Path: {path}")
    except FileNotFoundError:
        questionary.print(f"Path: {path}")


def view_latest_report() -> None:
    if not BENCHMARK_DIR.exists():
        questionary.print(f"No reports yet in {BENCHMARK_DIR}.", style="bold fg:yellow")
        return
    reports = sorted(
        (p for p in BENCHMARK_DIR.glob("*.md")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not reports:
        questionary.print(f"No reports yet in {BENCHMARK_DIR}.", style="bold fg:yellow")
        return
    choice = _select(
        "Pick a report (most recent first):",
        choices=[questionary.Choice(p.name, value=p) for p in reports[:25]]
        + [questionary.Choice("← back", value=_BACK)],
    ).ask()
    if choice is None or choice == _BACK:
        return
    _open_file(choice)


def run_optimization_demo() -> None:
    """optimization.py is a module, not a script. Show what it does."""
    questionary.print(
        "\noptimization.py is a scoring module, not a runnable script.",
        style="bold fg:cyan",
    )
    questionary.print(
        "It's used by run_benchmark.py to compare bytes scanned between\n"
        "agent SQL and peak SQL on cases where results match. To see its\n"
        "output, run a benchmark and check the 'Optimization' column in\n"
        "the report.\n"
    )


def generate_xlsx_template() -> None:
    _run([PYTHON, "scripts/generate_xlsx_template.py"])


def generate_reference_csvs() -> None:
    _run([PYTHON, "scripts/generate_reference_csvs.py"])


def generate_agent_template() -> None:
    _run([PYTHON, "scripts/generate_agent_template.py"])


def main() -> int:
    actions = {
        "Run a benchmark": run_benchmark,
        "Validate the judge": validate_judge,
        "Check calibration freshness": check_calibration,
        "View latest report": view_latest_report,
        "Regenerate agent input template (from golden_cases)": generate_agent_template,
        "Generate Sheets .xlsx template": generate_xlsx_template,
        "Generate reference CSVs": generate_reference_csvs,
        "About optimization.py": run_optimization_demo,
    }
    while True:
        choice = _select(
            "What do you want to do?",
            choices=list(actions.keys()) + ["Quit"],
        ).ask()
        if choice is None or choice == "Quit":
            return 0
        _header(choice)
        actions[choice]()
        _footer()


if __name__ == "__main__":
    sys.exit(main())
