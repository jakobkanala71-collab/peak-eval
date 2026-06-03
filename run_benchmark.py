"""peak_v2 — run a SQL agent against the golden corpus and produce a report.

Usage:
    .venv/bin/python run_benchmark.py corpus/golden_cases.csv inputs/<agent>.csv \\
        --agent-name model_v2

For each (golden, agent_input) pair:
    1. Execute peak_sql in BigQuery (dry-run cost check + cache off + row cap).
    2. Execute agent_sql in BigQuery (same guardrails).
    3. execution_match(peak_result, agent_result) → True/False.
    4. llm_judge(prompt, agent_explanation, peak_result, peak_sql, agent_sql, diff)
       → factual / complete / no_halluc + failure_category.
    5. Capture timing and bytes scanned for both.

Writes a markdown report to runs/benchmark/<agent>_<timestamp>.md and prints
the trust-weighted CLI scorecard at the end.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning, module=r"google\.cloud\.bigquery.*")
warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\.api_core.*")

from anthropic import Anthropic
from dotenv import load_dotenv

import bigquery_runner as bq
import input_fingerprint as fp
from corpus import load_agent_inputs, load_golden_cases, pair_cases
from judge import (
    JUDGE_MODEL,
    format_usage,
    get_usage,
    judge_prompt_hash,
    llm_judge,
    reset_usage,
)
from optimization import optimization_score
from report import (
    compute_scorecard,
    format_calibration_status,
    format_case_line,
    latest_calibration,
    render_benchmark_report,
    render_scorecard_cli,
    utc_timestamp,
    write_run,
)
from scoring import diff_results
from spinner import spinner

load_dotenv()

BENCHMARK_DIR = Path(__file__).resolve().parent / "runs" / "benchmark"
CALIBRATION_DIR = Path(__file__).resolve().parent / "runs" / "calibration"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the peak_v2 benchmark on an agent's outputs.")
    parser.add_argument("golden_csv", help="Path to corpus/golden_cases.csv")
    parser.add_argument("agent_csv", help="Path to inputs/<agent>.csv")
    parser.add_argument(
        "--agent-name",
        required=True,
        help="Identifier used in the report filename (e.g. model_v2)",
    )
    parser.add_argument(
        "--include-drafts",
        action="store_true",
        help="Include golden cases with status != 'approved' (default: only approved).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if this agent input was already benchmarked (skips the fingerprint check).",
    )
    args = parser.parse_args()

    golden_path = Path(args.golden_csv)
    agent_path = Path(args.agent_csv)

    if not golden_path.exists():
        print(f"❌ {golden_path} not found")
        return 1
    if not agent_path.exists():
        print(f"❌ {agent_path} not found")
        return 1

    components = fp.component_hashes(
        agent_path,
        judge_prompt_hash=judge_prompt_hash(),
        corpus_csv=golden_path,
    )
    fingerprint = fp.compute_fingerprint(
        agent_path,
        judge_prompt_hash=judge_prompt_hash(),
        corpus_csv=golden_path,
    )
    prior = fp.lookup(BENCHMARK_DIR, fingerprint)
    print(
        f"Run fingerprint: {fingerprint}  "
        f"(agent={components['agent_hash']}  corpus={components['corpus_hash']}  judge={components['judge_prompt_hash']})"
    )
    if prior and not args.force:
        print(
            f"❌ Identical run already benchmarked as "
            f"'{prior.agent_name}' on {prior.timestamp}."
        )
        print("   Same agent submission, same corpus, same judge prompt — nothing would change.")
        print(f"   Prior report: {prior.report_path}")
        print(f"   Prior source: {prior.source_csv}")
        print("   Edit the judge prompt or the corpus, or pass --force to re-run anyway.")
        return 1
    if prior and args.force:
        print(
            f"⚠ Overriding: identical run was previously benchmarked as "
            f"'{prior.agent_name}' on {prior.timestamp} (--force)."
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY not set (copy .env.example to .env)")
        return 1
    if not os.environ.get("BQ_PROJECT"):
        print("❌ BQ_PROJECT not set (copy .env.example to .env)")
        return 1

    try:
        goldens = load_golden_cases(golden_path, only_approved=not args.include_drafts)
    except ValueError as e:
        print(f"❌ Failed to load golden cases: {e}")
        return 1
    try:
        agent_inputs = load_agent_inputs(agent_path)
    except ValueError as e:
        print(f"❌ Failed to load agent inputs: {e}")
        return 1

    paired, unanswered, extra = pair_cases(goldens, agent_inputs)

    if unanswered:
        print(f"⚠ Goldens not answered by agent: {', '.join(unanswered)}")
    if extra:
        print(f"⚠ Agent inputs without matching golden: {', '.join(extra)}")
    if not paired:
        print("❌ No paired cases — nothing to score.")
        return 1

    bq_client = bq.make_client()
    anthropic_client = Anthropic()
    reset_usage()

    cal_status = latest_calibration(CALIBRATION_DIR)
    print(format_calibration_status(judge_prompt_hash(), cal_status))

    print(f"\nRunning {len(paired)} cases for agent='{args.agent_name}'...\n")

    rows: list[dict] = []
    for i, pair in enumerate(paired, 1):
        case_id = pair.golden.id
        prefix = f"  [{i}/{len(paired)}] {case_id}"

        row: dict = {
            "id": case_id,
            "tier": pair.golden.tier,
            "category": pair.golden.category,
            "prompt": pair.golden.prompt,
            "exec_match": False,
            "judge": None,
            "error": None,
            "peak_stats": None,
            "agent_stats": None,
        }

        try:
            with spinner(f"{prefix} → executing peak SQL", color="peak"):
                peak_df, peak_stats = bq.execute(pair.golden.peak_sql, client=bq_client)
            row["peak_stats"] = peak_stats.__dict__
        except bq.QueryError as e:
            row["error"] = f"peak_sql: {e}"
            print(f"{prefix} ❌ peak_sql: {e}", flush=True)
            rows.append(row)
            continue

        try:
            with spinner(f"{prefix} → executing agent SQL", color="agent"):
                agent_df, agent_stats = bq.execute(pair.agent.agent_sql, client=bq_client)
            row["agent_stats"] = agent_stats.__dict__
        except bq.QueryError as e:
            row["error"] = f"agent_sql: {e}"
            print(f"{prefix} ❌ agent_sql: {e}", flush=True)
            rows.append(row)
            continue

        diff = diff_results(peak_df, agent_df)
        row["exec_match"] = diff.matched
        row["exec_diff_summary"] = diff.summary
        row["exec_diff_detail"] = diff.detail

        row["optimization"] = optimization_score(
            peak_bytes=peak_stats.bytes_scanned,
            peak_duration_ms=peak_stats.duration_ms,
            agent_bytes=agent_stats.bytes_scanned,
            agent_duration_ms=agent_stats.duration_ms,
            exec_match=diff.matched,
        )

        try:
            with spinner(f"{prefix} → judging answer (Sonnet 4.6)", color="judge"):
                judge_result = llm_judge(
                    prompt=pair.golden.prompt,
                    agent_answer=pair.agent.agent_explanation,
                    peak_result_df=peak_df,
                    peak_sql=pair.golden.peak_sql,
                    agent_sql=pair.agent.agent_sql,
                    exec_diff_summary=diff.summary
                    + (("\n" + diff.detail) if diff.detail else ""),
                    client=anthropic_client,
                )
            row["judge"] = judge_result
            print(
                format_case_line(
                    index=i,
                    total=len(paired),
                    case_id=case_id,
                    exec_match=row["exec_match"],
                    judge=judge_result,
                    optimization=row.get("optimization"),
                ),
                flush=True,
            )
        except Exception as e:
            row["error"] = f"judge: {e}"
            print(f"{prefix} ❌ judge: {e}", flush=True)

        rows.append(row)

    scorecard = compute_scorecard(rows)
    usage = get_usage()

    print()
    print(render_scorecard_cli(scorecard, agent_name=args.agent_name, n_cases=len(rows)))
    print()
    print(f"Anthropic usage: {format_usage(usage)}")

    file_ts, iso_ts = utc_timestamp()
    body = render_benchmark_report(
        agent_name=args.agent_name,
        iso_ts=iso_ts,
        judge_model=JUDGE_MODEL,
        judge_hash=judge_prompt_hash(),
        rows=rows,
        scorecard=scorecard,
        usage_str=format_usage(usage),
        unanswered_prompts=unanswered,
        extra_inputs=extra,
    )
    out_path = BENCHMARK_DIR / f"{args.agent_name}_{file_ts}.md"
    write_run(out_path, body)

    fp.record(
        BENCHMARK_DIR,
        fingerprint=fingerprint,
        agent_name=args.agent_name,
        timestamp=iso_ts,
        report_path=str(out_path.relative_to(Path(__file__).resolve().parent))
        if out_path.is_relative_to(Path(__file__).resolve().parent)
        else str(out_path),
        source_csv=str(agent_path),
        agent_hash=components["agent_hash"],
        corpus_hash=components["corpus_hash"],
        judge_prompt_hash=components["judge_prompt_hash"],
    )

    rel = out_path.relative_to(Path.cwd()) if out_path.is_relative_to(Path.cwd()) else out_path
    print(f"\nReport saved to {rel}")
    return 0 if scorecard.verdict == "promote" else 0  # always 0 for now; verdict is signal, not gate


if __name__ == "__main__":
    sys.exit(main())
