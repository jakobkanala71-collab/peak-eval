# Peak Harness

**An evaluation harness for AI analytics agents.**

Peak Harness is *not* an SQL agent — it's the thing that **scores** SQL agents. You
give an agent a business question in plain English ("how many rides did we have
last month?"). The agent answers with **SQL + a written explanation**. Peak Harness runs
that SQL against a hand-curated *golden* answer, grades the explanation with an LLM,
measures cost, and prints a **trust-weighted scorecard** with a one-word verdict:
`promote` · `investigate` · `do not promote`.

The point is to answer one question honestly: **can we trust this agent's answers
enough to ship it?** — and to break that down so the cases that matter most aren't
averaged away by the easy ones.

---

## What it measures — three independent scorers

Each case is scored on three axes at once. They're deliberately independent, so a
failure tells you *which kind* of failure it is.

| Scorer | Question it answers | Failure it catches | How |
|---|---|---|---|
| **`execution_match`** | Did the agent's SQL return the **same data** as the golden SQL? | Wrong result | Deterministic — runs both queries in BigQuery and diffs the result sets (sort-invariant, float-tolerant). No LLM. |
| **`llm_judge`** | Does the agent's **explanation faithfully describe its own SQL** and answer the question? | Right result, hallucinated story | An LLM (Claude Sonnet) scores three binary dimensions: `factual_accuracy`, `completeness`, `no_hallucination`. |
| **`optimization`** | Is the agent's correct answer **cheaper or pricier** than the golden? | Expensive correct answers | Deterministic — compares **bytes scanned** (not wall-clock, which is noisy). `n/a` unless results match. |

> **Why the judge ignores SQL bugs.** The judge is told *not* to penalize the
> explanation just because the SQL is wrong — that's `execution_match`'s job. The
> judge only fails an explanation that **lies about what the SQL does** (claims a
> filter that isn't there, names the wrong table) or **invents** things not derivable
> from the data (trends, causes, recommendations). Clean separation: wrong number →
> `execution_match`; wrong *story* → `llm_judge`.

The judge also tags a **`failure_category`** when results differ
(`missing_filter`, `wrong_join`, `time_window`, `wrong_aggregation`, …) so the
scorecard can tell you *how* an agent tends to fail, not just that it did.

---

## How it orchestrates — the map

```
                         corpus/golden_cases.csv        inputs/<agent>.csv
                         (ground truth, per id)         (agent's SQL + explanation, per id)
                                  │                                │
                                  └──────────────┬─────────────────┘
                                                 ▼
                                      pair_cases()  ── match by `id`
                                                 │   (warns on unmatched on either side)
                                                 ▼
                              ┌──────────  for each paired case  ──────────┐
                              │                                            │
                              ▼                                            │
                   bigquery_runner.execute(peak_sql)                       │
                       • read-only check (SELECT/WITH only)                │
                       • dry-run cost check  (refuse > 1 GB scan)          │
                       • run, cache off      (refuse > 10k rows)           │
                       • capture bytes scanned + duration                  │
                              │                                            │
                   bigquery_runner.execute(agent_sql)  ◄── same guardrails │
                              │                                            │
                              ▼                                            │
        ┌─────────────────────┼─────────────────────┐                     │
        ▼                     ▼                     ▼                      │
  scoring.diff_results   judge.llm_judge      optimization_score           │
  (exec_match T/F     (Sonnet: 3 binary       (bytes-scanned              │
   + human diff)       dims + failure_cat)     better/worse/n.a.)          │
        └─────────────────────┼─────────────────────┘                     │
                              ▼                                            │
                       one result row  ─────────────────────────────────► │
                              └────────────────────────────────────────────┘
                                                 ▼
                                  report.compute_scorecard()
                          (groups rows by tier + category into aggregates)
                                                 ▼
                       ┌─────────────────────────────────────────────┐
                       │  TRUST-WEIGHTED SCORECARD                    │
                       │   • Headline correctness (T1)      ≥ 90%     │
                       │   • Trust-critical correctness     ≥ 85%     │
                       │   • Calibration (uncertainty)      ≥ 80%     │
                       │   • Efficiency ratio (median)                │
                       │   • Reasoning faithfulness (judge avg)       │
                       │   ── VERDICT: promote / investigate / don't  │
                       └─────────────────────────────────────────────┘
                                                 ▼
                          CLI printout  +  runs/benchmark/<agent>_<ts>.md
```

`run_benchmark.py` is the conductor that walks this top to bottom. Everything else
is a single-purpose module it calls.

### Module map

| File | Role |
|---|---|
| `peak.py` | Interactive menu (arrow-key TUI) — the default entry point; wraps every script below. |
| `run_benchmark.py` | Orchestrator / CLI. Loads, pairs, loops, aggregates, writes the report. |
| `corpus.py` | Loads + validates `golden_cases.csv` and the agent input CSV; pairs them by `id`. |
| `categories.py` | The 12 case categories and their trust-risk classification (what's "trust-critical" vs "calibration"). |
| `bigquery_runner.py` | Runs SQL safely: read-only check, dry-run cost ceiling, row cap, stats capture. |
| `scoring.py` | `execution_match` + a human-readable result diff. |
| `judge.py` | The LLM judge: prompt, the 3 binary dimensions, `failure_category`, token/cost accounting. |
| `optimization.py` | Bytes-scanned efficiency comparison (and a "is the golden itself improvable?" flag). |
| `report.py` | Builds the trust-weighted scorecard + verdict; renders the CLI output and the markdown report. |
| `input_fingerprint.py` | Hashes (agent input + corpus + judge prompt) so an identical run is refused — no wasted API spend. |
| `validate_judge.py` / `check_calibration.py` | Calibrate the judge against hand-scored cases; verify the calibration is still fresh. |
| `spinner.py` | Terminal progress spinner. |

---

## Tiers & the trust-weighted scorecard

A single average hides the cases that matter most, so every case carries a **tier**
and a **category**, and the scorecard reports separate aggregates with their own
thresholds:

- **T1 — Floor.** Trivial, hard to fail. → **Headline correctness**, threshold 90%.
- **T2 — Main signal.** Unambiguous but hard. The biggest bucket. Trust-critical
  categories here feed **Trust-critical correctness**, threshold 85%.
- **T3 — Judgment.** Ambiguous *by design*. Tests whether the agent surfaces its
  assumption / refuses honestly. → **Calibration**, threshold 80% (judged on the
  explanation, not on `execution_match`).

**Verdict logic:** trust-critical *or* calibration below threshold → `do not
promote`. Only headline below → `investigate`. All met → `promote`. (Too few cases
in an aggregate → `incomplete`.)

The 12 categories and which aggregate they feed live in `categories.py` and
`templates/sheet_tabs/categories.csv`.

---

## Guardrails

| Limit | Value | Where |
|---|---|---|
| BigQuery scan ceiling per query | 1 GB (dry-run enforced) | `bigquery_runner.py` |
| Result row cap per query | 10,000 | `bigquery_runner.py` |
| Read-only SQL (SELECT/WITH only) | enforced before the query leaves the process | `bigquery_runner.py` |
| LLM calls per run | 75 | `judge.py` |
| Judge output tokens per call | 512 | `judge.py` |

> Read-only is enforced in code as a backstop — the real guarantee is IAM. Run the
> benchmark with a BigQuery identity that has **Job User + Data Viewer only**.

---

## Setup

```bash
# 1. Virtual env + deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Auth to BigQuery
gcloud auth application-default login

# 3. Env
cp .env.example .env
#   edit .env: set ANTHROPIC_API_KEY and BQ_PROJECT
```

## Run it — the menu

The normal way to drive everything is the interactive menu:

```bash
.venv/bin/python peak.py
```

It's an arrow-key TUI that wraps every task, so you don't have to remember
flags or paths:

- **Run a benchmark** — pick an agent CSV from `inputs/`, name the run, choose
  whether to include drafts. Warns (with the prior report) if you're about to
  re-run an identical agent/corpus/judge combination.
- **Validate the judge** / **Check calibration freshness**
- **View latest report** — opens a past run from `runs/benchmark/`
- **Regenerate agent input template**, **Generate Sheets .xlsx / reference CSVs**

The menu expects your reviewed corpus at `corpus/golden_cases.csv` (copy it from
the template — see [The corpus](#the-corpus-ground-truth) below) and agent
submissions in `inputs/`.

### Scriptable alternative (CI, automation)

Every menu action maps to a script you can call directly — useful for CI:

```bash
.venv/bin/python run_benchmark.py corpus/golden_cases.csv inputs/<agent>.csv \
    --agent-name <agent>
```

Prints the scorecard and writes a full report to `runs/benchmark/<agent>_<timestamp>.md`.
(Add `--include-drafts` to score `status: draft` cases too; add `--force` to re-run
an identical, already-benchmarked combination.)

## The run report — per-case reasoning

Every run writes a markdown report to `runs/benchmark/<agent>_<timestamp>.md`. Above
the headline scorecard it carries a **per-case breakdown** so you can see *why* each
case scored the way it did — not just a pass/fail tally. For each case it records:

- the **prompt**, tier, and category
- **execution_match**, and when results differ, the exec-diff (which columns, rows,
  or values diverged)
- the judge's three dimension scores (factual / complete / no_halluc) and the
  **failure category** (`wrong_join`, `time_window`, …)
- **Judge reasoning** — the judge's own free-text account of *what it looked at and
  why it scored that way*, citing the specific SQL operations and explanation phrases
- the optimization verdict plus per-query bytes-scanned and timing

This is the artifact to open when a score surprises you, or to hand an agent owner as
actionable feedback. (Reports can contain real query results, so `runs/` is gitignored
by default — see [the honesty note](#a-note-on-honesty).)

---

## The corpus (ground truth)

> **These are placeholders — replace them.** Everything shipped in this repo (the
> golden cases here *and* the judge calibration cases below) is fictional sample
> data in a made-up **bike-share** domain, included only to show the structure. It
> is not meant to be run as-is. Swap in cases from your own warehouse before you
> trust any score.

`corpus/golden_cases_template.csv` is a ready-to-edit template with 16 fictional
example cases — one or more per tier and category — using a placeholder `demo.*`
schema. **Replace the SQL with real queries against your own warehouse**, write the
`golden_reasoning` for each, have an analyst review it, and flip cases to
`status: approved` when reviewed. A golden case is "golden" only because a human
signed off on it — the template rows are all `status: draft` on purpose.

Each row: `id, tier, category, domain, prompt, peak_sql, golden_reasoning, tables,
sql_features, ambiguity_flag, evidence_required, status`. Only `status: approved`
rows are scored by default.

Typical authoring loop:

```bash
# Generate a fresh starter corpus + templates from scratch
.venv/bin/python scripts/generate_starter_corpus.py     # writes corpus/golden_cases.csv + templates
.venv/bin/python scripts/generate_reference_csvs.py     # writes the Sheet reference tabs
.venv/bin/python scripts/generate_xlsx_template.py      # (optional) one .xlsx to import into Google Sheets
```

Many teams author the corpus in a Google Sheet (the editing surface) and download
the `golden_cases` tab to `corpus/golden_cases.csv` (the runtime source), committed
to git so each benchmark run references a stable corpus version. The reference tabs
(`templates/sheet_tabs/`) document the tiers and categories for authors.

## Submitting an agent's output

The agent owner produces a CSV with three columns: `id, agent_sql,
agent_explanation` — one row per golden-case id they answer. Start from
`templates/agent_input_template.csv` (or see `templates/example_agent_input.csv` for
a filled example), drop it in `inputs/<agent>.csv`, and run the benchmark.

## Calibrate the judge

The LLM judge is only trustworthy if it scores the way a human would. Calibration
checks that: it runs the judge on a set of cases whose scores a **human has already
assigned by hand**, then reports how often the judge agrees (per dimension, plus
Cohen's κ to discount chance agreement). Each dimension should agree ≥ 85%.

```bash
.venv/bin/python validate_judge.py      # runs the hand-scored cases; each dim should agree ≥ 85%
.venv/bin/python check_calibration.py   # no API calls — just checks the calibration is still fresh
```

> **The calibration cases are placeholders too — replace them with your own.** The
> ~12 cases in `validate_judge.py` (`VALIDATION_CASES`) are fictional bike-share
> examples, each **hand-scored** with the labels a human reviewer judged correct
> (`human_factual`, `human_complete`, `human_no_halluc`, `human_failure_category`).
> They exist to show the shape. Before you rely on the judge, replace them with cases
> from *your* domain and sit down with an analyst to assign the human scores — that
> hand-scoring is the whole point. The synthetic set proves the rubric is internally
> coherent; it does not prove the judge handles your real, messy cases. Aim for cases
> that span each dimension and each failure mode (a clean case, a misstated filter, an
> unsupported trend claim, a vague dodge, etc.).

The judge prompt is hashed, so a calibration is tied to the exact prompt version it
was measured against; editing `JUDGE_PROMPT` flags the old calibration as stale (and
you should re-run `validate_judge.py`).

---

## Roadmap

The harness is a deliberately thin shell — a correct scoring core plus a seed corpus —
meant to be extended as real agent failures surface. Planned work (deeper deterministic
signals, multi-judge agreement with Cohen's κ as the real gate, agent confidence
calibration, versioned/reproducible runs, CI mode, and more) lives in
[`ROADMAP.md`](./ROADMAP.md).

---

## A note on honesty

Peak Harness is a **proxy** for *relative* agent quality, not a proof of absolute
correctness. The golden standard isn't gold — a strong agent will sometimes produce
SQL that's cleaner or more correct than the reference. When `execution_match` fails
and inspection shows the agent is right, you update the golden. The corpus improves
over time *because* good agents push back on it. The harness is "right" when its
scores match how a human analyst would rate the agents; when they don't, you fix the
harness — not the score.
