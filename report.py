"""Markdown report writer + trust-weighted aggregates for peak_v2.

The CLI summary mirrors the trust-weighted scorecard from
benchmark_cases.html:

    Headline correctness (T1)              — does the agent handle easy cases?
    Trust-critical correctness             — does it handle the cases that matter most?
    Calibration                            — does it surface uncertainty / refuse?
    Efficiency ratio (median, on correct)  — is it cheap or expensive at being right?
    Reasoning faithfulness (judge avg)     — does the explanation match the SQL?
    Failures concentrated in               — which categories cluster the failures?

A go/no-go thresholds: headline ≥ 90%, trust-critical ≥ 85%, calibration ≥ 80%.
Override the thresholds via env vars if needed (PEAK_HEADLINE_THRESHOLD, etc.).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from categories import CALIBRATION_SLUGS, CATEGORY_BY_SLUG, TRUST_CRITICAL_SLUGS

HEADLINE_THRESHOLD = float(os.environ.get("PEAK_HEADLINE_THRESHOLD", "0.90"))
TRUST_CRITICAL_THRESHOLD = float(os.environ.get("PEAK_TRUST_CRITICAL_THRESHOLD", "0.85"))
CALIBRATION_THRESHOLD = float(os.environ.get("PEAK_CALIBRATION_THRESHOLD", "0.80"))

# Minimum cases per threshold aggregate before a verdict is statistically
# interpretable. At N=5 binary, P(all-pass under random chance) = 0.5^5 = 0.031,
# the smallest sample to reject pure-chance at alpha=0.05. Below this, the
# aggregate cannot support a promote/don't-promote claim.
MIN_CASES_PER_AGGREGATE = int(os.environ.get("PEAK_MIN_CASES_PER_AGGREGATE", "5"))

RULE = "─" * 70
HEAVY_RULE = "━" * 70
DOUBLE_RULE = "═" * 70
LABEL_WIDTH = 38


# ---------------------------------------------------------------------------
# ANSI helpers — colour only when stdout is a TTY, otherwise plain text.
# ---------------------------------------------------------------------------

def _ansi(text: str, code: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text


def green(t: str) -> str: return _ansi(t, "32")
def red(t: str) -> str: return _ansi(t, "31")
def yellow(t: str) -> str: return _ansi(t, "33")
def cyan(t: str) -> str: return _ansi(t, "36")
def magenta(t: str) -> str: return _ansi(t, "35")
def dim(t: str) -> str: return _ansi(t, "2")
def bold(t: str) -> str: return _ansi(t, "1")
def bold_green(t: str) -> str: return _ansi(t, "1;32")
def bold_red(t: str) -> str: return _ansi(t, "1;31")
def bold_yellow(t: str) -> str: return _ansi(t, "1;33")


def status_symbol(passed: bool) -> str:
    return green("✓") if passed else red("✗")


def utc_timestamp() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d_%H%M%S"), now.isoformat(timespec="seconds")


def write_run(path: Path, body: str) -> Path:
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Calibration freshness — same logic as peak/check_calibration.py
# ---------------------------------------------------------------------------

_CALIBRATION_FILENAME_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{6})_(?P<hash>[0-9a-f]{8})\.md$"
)
_AGREEMENT_LINE_RE = re.compile(r"\*\*Overall agreement:\*\*\s*(\d+)%")


@dataclass
class CalibrationStatus:
    timestamp: str
    prompt_hash: str
    agreement_pct: int | None
    path: Path

    @property
    def date(self) -> str:
        return self.timestamp.split("_")[0]


def latest_calibration(calibration_dir: Path) -> CalibrationStatus | None:
    if not calibration_dir.exists():
        return None
    candidates: list[tuple[str, str, Path]] = []
    for path in calibration_dir.glob("*.md"):
        m = _CALIBRATION_FILENAME_RE.match(path.name)
        if not m:
            continue
        candidates.append((m["ts"], m["hash"], path))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    ts, prompt_hash, path = candidates[0]

    agreement_pct: int | None = None
    try:
        body = path.read_text()
        m = _AGREEMENT_LINE_RE.search(body)
        if m:
            agreement_pct = int(m.group(1))
    except OSError:
        pass

    return CalibrationStatus(
        timestamp=ts, prompt_hash=prompt_hash, agreement_pct=agreement_pct, path=path
    )


def format_calibration_status(current_prompt_hash: str, status: CalibrationStatus | None) -> str:
    if status is None:
        return "⚠ Judge not yet calibrated — run validate_judge.py before trusting results."
    agreement = f"{status.agreement_pct}%" if status.agreement_pct is not None else "?"
    if status.prompt_hash == current_prompt_hash:
        return f"Judge calibrated: {status.date} ({agreement} agreement, current prompt)"
    return (
        f"⚠ Judge calibration is stale: last calibrated {status.date} against prompt "
        f"`{status.prompt_hash}` ({agreement} agreement); current prompt is "
        f"`{current_prompt_hash}`. Run validate_judge.py."
    )


# ---------------------------------------------------------------------------
# Trust-weighted aggregates
# ---------------------------------------------------------------------------

@dataclass
class Aggregate:
    """A single rate aggregate (numerator/denominator)."""
    name: str
    passed: int
    total: int
    threshold: float | None = None

    @property
    def rate(self) -> float | None:
        return self.passed / self.total if self.total else None

    @property
    def status(self) -> str:
        """'ok' | 'warn' | 'na'."""
        if self.rate is None:
            return "na"
        if self.threshold is None:
            return "ok"
        return "ok" if self.rate >= self.threshold else "warn"

    def format(self) -> str:
        """Plain markdown-friendly format, used in the .md report (no ANSI)."""
        if self.total == 0:
            return f"{self.name}: — (no cases)"
        rate_str = f"{self.rate:.0%}" if self.rate is not None else "—"
        marker = ""
        if self.threshold is not None and self.rate is not None:
            marker = "  ✓" if self.rate >= self.threshold else f"  ⚠ below {self.threshold:.0%}"
        return f"{self.name}: {rate_str} ({self.passed}/{self.total}){marker}"

    def format_cli(self) -> str:
        """Aligned, ANSI-colored line for the terminal scorecard."""
        label = self.name.ljust(LABEL_WIDTH)
        if self.total == 0:
            return f"  {label}—       (no cases)"
        rate_str = f"{self.rate:.0%}".rjust(4)
        counts = f"({self.passed} / {self.total})".ljust(13)
        if self.threshold is None or self.rate is None:
            marker = ""
        elif self.rate >= self.threshold:
            marker = green("✓")
        else:
            marker = yellow(f"⚠ below {self.threshold:.0%}")
        return f"  {label}{rate_str}   {counts} {marker}"


@dataclass
class Scorecard:
    headline: Aggregate
    trust_critical: Aggregate
    calibration: Aggregate
    efficiency_median_ratio: float | None  # median(min(peak_bytes/agent_bytes, 1.0))
    faithfulness_avg: float | None  # 0..3
    failures_top_categories: list[tuple[str, int]]  # (category_slug, count)

    @property
    def insufficient_aggregates(self) -> list[str]:
        """Aggregates with too few cases to support a verdict claim."""
        out = []
        if self.headline.total < MIN_CASES_PER_AGGREGATE:
            out.append("headline")
        if self.trust_critical.total < MIN_CASES_PER_AGGREGATE:
            out.append("trust-critical")
        if self.calibration.total < MIN_CASES_PER_AGGREGATE:
            out.append("calibration")
        return out

    @property
    def verdict(self) -> str:
        """'promote' | 'investigate' | 'do_not_promote' | 'incomplete'.

        Returns 'incomplete' when any threshold aggregate has fewer than
        MIN_CASES_PER_AGGREGATE judged cases — i.e. the corpus is too thin
        to make a statistically meaningful claim, regardless of pass rates.
        """
        if self.insufficient_aggregates:
            return "incomplete"

        h = self.headline
        t = self.trust_critical
        c = self.calibration
        below = []
        if h.status == "warn":
            below.append("headline")
        if t.status == "warn":
            below.append("trust-critical")
        if c.status == "warn":
            below.append("calibration")
        if not below:
            return "promote"
        if "trust-critical" in below or "calibration" in below:
            return "do_not_promote"
        return "investigate"

    def verdict_message(self) -> str:
        if self.verdict == "incomplete":
            missing = ", ".join(self.insufficient_aggregates)
            return (
                f"VERDICT: incomplete — aggregate(s) below minimum case count "
                f"({MIN_CASES_PER_AGGREGATE}): {missing}. Corpus is too thin to "
                f"support a ship/no-ship claim."
            )
        if self.verdict == "promote":
            return "VERDICT: ok to promote — all aggregates above threshold."
        if self.verdict == "do_not_promote":
            return (
                "VERDICT: do not promote — trust-critical or calibration below "
                "threshold; investigate before re-scoring."
            )
        return (
            "VERDICT: investigate — headline correctness below threshold but "
            "trust-critical and calibration look ok."
        )


def compute_scorecard(rows: list[dict]) -> Scorecard:
    headline_total = headline_pass = 0
    tc_total = tc_pass = 0
    cal_total = cal_pass = 0
    fail_by_cat: dict[str, int] = {}
    eff_ratios: list[float] = []
    judge_total = 0
    judge_dim_sum = 0  # sum of factual + completeness + no_halluc

    for r in rows:
        category = r.get("category")
        tier = r.get("tier")
        exec_match = bool(r.get("exec_match"))
        judge = r.get("judge")
        peak_stats = r.get("peak_stats") or {}
        agent_stats = r.get("agent_stats") or {}

        # Headline = T1 (regardless of category)
        if tier == "T1":
            headline_total += 1
            if exec_match:
                headline_pass += 1

        # Trust-critical = category in TRUST_CRITICAL_SLUGS, any tier
        if category in TRUST_CRITICAL_SLUGS:
            tc_total += 1
            if exec_match:
                tc_pass += 1

        # Calibration = category in CALIBRATION_SLUGS. "Pass" = judge gave 1/1/1
        # (agent surfaced the assumption / refused honestly, however SQL went).
        if category in CALIBRATION_SLUGS:
            cal_total += 1
            if judge and (
                judge.get("factual_accuracy")
                and judge.get("completeness")
                and judge.get("no_hallucination")
            ):
                cal_pass += 1

        # Failure breakdown by category. For trust-critical / floor cases,
        # exec_match=False is unambiguously a failure. For calibration cases
        # (ambiguity / refusal), exec_match might fail simply because the agent
        # picked a different valid interpretation; if the judge says the
        # explanation is sound (1/1/1), that's not a real failure.
        if not exec_match and category:
            judge_passed = bool(judge and (
                judge.get("factual_accuracy")
                and judge.get("completeness")
                and judge.get("no_hallucination")
            ))
            if category in CALIBRATION_SLUGS and judge_passed:
                pass  # judge says agent handled the ambiguity / refusal well
            else:
                fail_by_cat[category] = fail_by_cat.get(category, 0) + 1

        # Efficiency ratio: capped at 1.0 (an agent can't "win" by being more
        # efficient than golden — only by matching the result and being at-least-as-cheap)
        if exec_match and peak_stats and agent_stats:
            pb = peak_stats.get("bytes_scanned") or 0
            ab = agent_stats.get("bytes_scanned") or 0
            if pb > 0 and ab > 0:
                ratio = min(pb / ab, 1.0)
                eff_ratios.append(ratio)

        # Faithfulness: judge dim sum across all judged cases
        if judge:
            judge_total += 1
            judge_dim_sum += int(bool(judge.get("factual_accuracy")))
            judge_dim_sum += int(bool(judge.get("completeness")))
            judge_dim_sum += int(bool(judge.get("no_hallucination")))

    headline = Aggregate("Headline correctness (T1)", headline_pass, headline_total, HEADLINE_THRESHOLD)
    trust_critical = Aggregate("Trust-critical correctness", tc_pass, tc_total, TRUST_CRITICAL_THRESHOLD)
    calibration = Aggregate("Calibration (uncertainty / refusal)", cal_pass, cal_total, CALIBRATION_THRESHOLD)

    eff = median(eff_ratios) if eff_ratios else None
    faith = (judge_dim_sum / judge_total) if judge_total else None

    top = sorted(fail_by_cat.items(), key=lambda kv: -kv[1])[:3]
    return Scorecard(
        headline=headline,
        trust_critical=trust_critical,
        calibration=calibration,
        efficiency_median_ratio=eff,
        faithfulness_avg=faith,
        failures_top_categories=top,
    )


def render_scorecard_cli(sc: Scorecard, *, agent_name: str, n_cases: int) -> str:
    """Aligned, ANSI-colored scorecard for the terminal — designed for at-a-glance reading."""
    title = f"peak_v2  ·  SCORECARD"
    subtitle = f"agent: {bold(agent_name)}   ·   {n_cases} cases"

    lines: list[str] = [
        "",
        bold(cyan(HEAVY_RULE)),
        bold(f"  {title}"),
        f"  {dim(subtitle)}",
        bold(cyan(HEAVY_RULE)),
        "",
        dim("  Pass / fail aggregates"),
        sc.headline.format_cli(),
        sc.trust_critical.format_cli(),
        sc.calibration.format_cli(),
        "",
        dim("  Quality aggregates"),
    ]

    eff_label = "Efficiency ratio (median, correct)".ljust(LABEL_WIDTH)
    if sc.efficiency_median_ratio is not None:
        lines.append(f"  {eff_label}{sc.efficiency_median_ratio:.2f}")
    else:
        lines.append(f"  {eff_label}{dim('— (no correct cases with byte stats)')}")

    faith_label = "Reasoning faithfulness (judge avg)".ljust(LABEL_WIDTH)
    if sc.faithfulness_avg is not None:
        lines.append(f"  {faith_label}{sc.faithfulness_avg:.2f} / 3")
    else:
        lines.append(f"  {faith_label}{dim('— (no judged cases)')}")

    if sc.failures_top_categories:
        lines.append("")
        lines.append(dim("  Failures concentrated in"))
        total_fails = sum(c for _, c in sc.failures_top_categories)
        for slug, count in sc.failures_top_categories:
            title_str = CATEGORY_BY_SLUG[slug].title if slug in CATEGORY_BY_SLUG else slug
            share = (count / total_fails) if total_fails else 0
            bar_width = 20
            filled = int(round(share * bar_width))
            bar = "█" * filled + dim("░" * (bar_width - filled))
            lines.append(f"     {title_str.ljust(30)} {bar} {share:.0%}")

    lines.append("")
    lines.append(RULE)
    if sc.verdict == "incomplete":
        missing = ", ".join(sc.insufficient_aggregates)
        verdict_line = bold_yellow("  ◌  VERDICT: INCOMPLETE")
        reason = dim(
            f"     {missing} below minimum {MIN_CASES_PER_AGGREGATE} cases — corpus too thin for a ship claim."
        )
    elif sc.verdict == "promote":
        verdict_line = bold_green("  ✓  VERDICT: OK TO PROMOTE")
        reason = dim("     all aggregates pass their thresholds — safe to ship.")
    elif sc.verdict == "do_not_promote":
        verdict_line = bold_red("  ✗  VERDICT: DO NOT PROMOTE")
        reason = dim("     trust-critical or calibration below threshold — do not ship.")
    else:
        verdict_line = bold_yellow("  !  VERDICT: INVESTIGATE")
        reason = dim("     headline (T1) below threshold, but trust-critical and calibration ok — worth a look, not a blocker.")
    lines.append(verdict_line)
    lines.append(reason)
    lines.append(bold(cyan(HEAVY_RULE)))
    return "\n".join(lines)


def format_case_line(
    *,
    index: int,
    total: int,
    case_id: str,
    exec_match: bool,
    judge: dict | None,
    optimization: str | None,
) -> str:
    """One aligned line per case for the terminal — matches the HTML mockup."""
    prefix = dim(f"  [{index:03d}/{total:03d}]")
    label = case_id.ljust(28)[:28]
    status = status_symbol(exec_match)
    if judge:
        judge_str = (
            f"{judge['factual_accuracy']}/{judge['completeness']}/{judge['no_hallucination']}"
        )
        fail_cat = judge.get("failure_category") or ""
        fail_part = yellow(fail_cat) if fail_cat else ""
    else:
        judge_str = "-/-/-"
        fail_part = ""
    opt_label = (optimization or "-").ljust(11)
    return f"{prefix}  {label} {status}  judge {judge_str}   opt {opt_label} {fail_part}"


# ---------------------------------------------------------------------------
# Markdown report writer
# ---------------------------------------------------------------------------

def render_benchmark_report(
    *,
    agent_name: str,
    iso_ts: str,
    judge_model: str,
    judge_hash: str,
    rows: list[dict[str, Any]],
    scorecard: Scorecard,
    usage_str: str,
    unanswered_prompts: list[str],
    extra_inputs: list[str],
) -> str:
    lines = [
        f"# peak_v2 benchmark run — {agent_name} — {iso_ts}",
        "",
        f"- **Agent:** `{agent_name}`",
        f"- **Cases scored:** {len(rows)}",
        f"- **Judge model:** {judge_model}",
        f"- **JUDGE_PROMPT hash:** `{judge_hash}`",
        f"- **Anthropic usage:** {usage_str}",
        "",
        "## Trust-weighted scorecard",
        "",
        f"- **{scorecard.headline.format()}**",
        f"- **{scorecard.trust_critical.format()}**",
        f"- **{scorecard.calibration.format()}**",
    ]
    if scorecard.efficiency_median_ratio is not None:
        lines.append(
            f"- **Efficiency ratio (median, on correct):** {scorecard.efficiency_median_ratio:.2f}"
        )
    if scorecard.faithfulness_avg is not None:
        lines.append(
            f"- **Reasoning faithfulness (judge avg):** {scorecard.faithfulness_avg:.2f} / 3"
        )
    if scorecard.failures_top_categories:
        parts = []
        for slug, count in scorecard.failures_top_categories:
            title = CATEGORY_BY_SLUG[slug].title if slug in CATEGORY_BY_SLUG else slug
            parts.append(f"{title} ({count})")
        lines.append(f"- **Failures concentrated in:** {', '.join(parts)}")
    lines.append("")
    lines.append(f"> {scorecard.verdict_message()}")
    lines.append("")

    if unanswered_prompts:
        lines += [f"⚠ **Prompts not answered by agent:** {', '.join(unanswered_prompts)}", ""]
    if extra_inputs:
        lines += [f"⚠ **Agent inputs without matching golden:** {', '.join(extra_inputs)}", ""]

    lines += ["## Per-case detail", ""]
    for r in rows:
        lines.append(f"### {r['id']}  ·  {r.get('tier','?')}  ·  {r.get('category','?')}")
        lines.append("")
        lines.append(f"**Prompt:** {r.get('prompt','')}")
        lines.append("")
        if r.get("error"):
            lines.append(f"**❌ Error:** {r['error']}")
            lines.append("")
            continue

        lines.append(f"**execution_match:** {r['exec_match']}")
        if r.get("exec_diff_summary") and not r["exec_match"]:
            lines.append("")
            lines.append(f"**exec-diff:** {r['exec_diff_summary']}")
            if r.get("exec_diff_detail"):
                lines.append("")
                lines.append("```")
                lines.append(r["exec_diff_detail"])
                lines.append("```")
        lines.append("")

        if r.get("judge"):
            j = r["judge"]
            lines.append(
                f"**Judge:** factual={j['factual_accuracy']}, "
                f"complete={j['completeness']}, no_halluc={j['no_hallucination']}"
            )
            if j.get("failure_category"):
                lines.append(f"**Failure category:** `{j['failure_category']}`")
            lines.append("")
            lines.append(f"**Judge reasoning:** {j['reasoning']}")
            lines.append("")

        if r.get("optimization"):
            lines.append(f"**Optimization:** `{r['optimization']}`")
            lines.append("")

        peak_stats = r.get("peak_stats")
        agent_stats = r.get("agent_stats")
        if peak_stats:
            lines.append(
                f"**peak query:** {peak_stats['duration_ms']:.0f} ms · "
                f"{peak_stats['bytes_scanned'] / 1e6:.1f} MB scanned · "
                f"{peak_stats['row_count']} rows"
            )
        if agent_stats:
            lines.append(
                f"**agent query:** {agent_stats['duration_ms']:.0f} ms · "
                f"{agent_stats['bytes_scanned'] / 1e6:.1f} MB scanned · "
                f"{agent_stats['row_count']} rows"
            )
        lines.append("")

    return "\n".join(lines)
