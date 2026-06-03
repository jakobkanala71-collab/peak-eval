# Peak Harness — Roadmap

What I plan to build next, roughly ordered by impact. The harness is intentionally a
shell: a thin, correct scoring core plus a seed corpus. It's meant to be *extended* as
real agent failures surface, not finished up front.

**Status:** `planned` unless marked `in progress` (= partially built, not yet wired in).

---

## 1. Deeper & more deterministic scoring

Today the deterministic signal is `execution_match` (same result?) + bytes scanned.
I want more, because deterministic signals don't cost an LLM call and don't drift.

- **Sub-signals instead of one boolean.** Break `execution_match` into column-set match,
  row-count match, and value match as separate reported signals — "right columns, wrong
  rows" is different feedback from "wrong shape entirely."
- **Gold-table provenance.** The same answer can come from a canonical table or a
  non-canonical one ("right number, wrong source"). Define canonical tables; score the
  fraction of an agent's referenced tables that are canonical.
- **Source-freshness tier.** Flag when the agent reads a coarser/staler source than the
  golden (daily-batch vs streaming) — same result on history, divergent on recent data.
- **Schema-equivalence fallback.** When the agent's SQL differs but is *defensibly*
  correct (it out-thought the golden), a second judge pass instead of auto-failing. This
  is the harness's biggest blind spot today.
- **Flag when the agent beats the corpus.** `in progress` — `optimization.should_review_peak_sql`
  already detects `exec_match=True` + agent bytes ≥10% lower + judge 1/1/1. Just needs
  wiring into the report as a "peak SQL review candidates" section.

## 2. Judge rigor & agreement

- **Multi-judge ensemble.** Run N judge prompts/models and look at the *spread*, not just
  the average. Convergence is signal; disagreement is a flag for human review.
- **Make Cohen's κ the actual gate.** `in progress` — κ is already computed in
  `validate_judge.py`, but the pass/fail decision still keys off *raw agreement ≥85%*,
  which is exactly what κ exists to protect against (with imbalanced labels, a judge that
  always says "1" can score 90% agreement at κ≈0). So κ is currently decorative. The work:
  - Gate on κ (≥0.6 substantial) per dimension, not raw agreement.
  - Bootstrap a confidence interval on κ; refuse a "calibrated" verdict when N is too small
    and the interval is too wide.
  - Add **judge-vs-judge κ** for the ensemble ("do the judges agree beyond chance?").
  - Stratify κ per dimension **and** per domain, so "great on operations, weak on finance"
    can't hide inside an aggregate.
- **Calibration drift tracking.** Append every calibration run to a history file
  (timestamp, judge hash, per-dimension agreement + κ) so I can see the judge slowly going
  out of calibration as the corpus grows.

## 3. Agent confidence calibration

The whole tool is about trust, and the cardinal trust sin is being *confidently wrong*.
So the agent should emit a confidence per answer, and I should score how well-calibrated
that confidence is.

- Add a `confidence` field to the agent output contract (see §6).
- Score the **confidence × correctness** matrix asymmetrically:

  | | correct | wrong |
  |---|---|---|
  | **high confidence** | ideal | **worst — confidently wrong** |
  | **low confidence** | fine (right but hedged) | acceptable (it flagged doubt) |

- Roll it into real metrics: **Brier score / ECE** (is the confidence actually
  calibrated?) and **selective accuracy** (accuracy on just the high-confidence answers —
  what you'd really deploy on).
- New scorecard aggregate: **Confidence calibration**, weighting confident-wrong heaviest.
  This generalizes today's calibration aggregate (which only asks "did it surface
  uncertainty on T3?") to every case via an explicit number.

## 4. Honest verdicts on small N

Early on, the corpus is small and the promote/investigate/don't verdict is noisy. The
verdict should admit that.

- **Wilson confidence intervals** on each pass-rate aggregate.
- Don't render a hard verdict when the interval straddles the threshold — fall back to
  `incomplete` / `investigate`. The verdict earns authority only as the corpus fills with
  real, hard-won cases.

## 5. Versioning & reproducibility

I want any past run to be exactly reproducible and comparable to the exact model + harness
state that produced it.

- **Stamp every report with the harness version** (semver + git SHA) alongside the
  existing corpus hash, judge-prompt hash, judge model, and agent hash. A run becomes
  fully pinned.
- **Tag releases + keep a CHANGELOG**, so I can check out an old harness version, re-run a
  past agent submission, and get byte-identical scoring.
- **Golden-answer provenance.** When a golden changes (because an agent out-thought it),
  record who/when/why — otherwise the corpus drifts silently and old reports stop being
  interpretable.

## 6. Integration & workflow

- **External agents via API.** Publish an **output contract** — the `id, agent_sql,
  agent_explanation, confidence` columns plus guidance ("describe the methodology, don't
  quote numbers") — so any agent can produce scorable output. Optionally a thin adapter
  that calls an agent endpoint and collects outputs instead of hand-built CSVs.
- **Agent-effort capture.** Once agents come in via API, record retries / tokens /
  latency. Today two agents with identical SQL score identically even if one flailed five
  times to get there.
- **CI-friendly mode.** `--strict` exit code (non-zero on `do_not_promote`), headless /
  no-TTY operation, machine-readable JSON output, and a sample GitHub Action.
- **Run-to-run diff.** Compare two reports and highlight per-case regressions and
  improvements — the only honest way to back a "v2 is better" claim.
- **Low-friction case capture.** A menu action that promotes a flagged benchmark case into
  a pre-filled draft `golden_cases.csv` row, so turning a real observed miss into a
  permanent regression case takes seconds, not minutes. (Pairs with a human-in-the-loop
  review queue for judge disagreements and T3 misses.)

## 7. Corpus tooling

- **Coverage report.** A tier × category × domain matrix — used as a *diagnostic* ("our
  failures are clustering in temporal_logic and we have zero finance temporal cases"), not
  a target to pad toward.
- **Golden-case linter.** Heuristics for ambiguous wording, `peak_sql` columns vs prompt
  entities mismatch, missing `golden_reasoning`, duplicate prompts on different ids.

## 8. Performance & portability

- **Parallel case execution.** Cases run serially today; a `ThreadPoolExecutor` around the
  per-case loop (capped to respect the Anthropic-calls guardrail) cuts wall-clock a lot.

---

When an item ships, strike it through and link the PR:

`### ~~1. …~~  — shipped #42`
