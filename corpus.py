"""Load and pair golden_cases.csv + agent_input.csv.

CSV-based corpus, exported from a Google Sheet. Schema is enforced at load
time so a malformed sheet fails loudly instead of producing silent garbage.

Golden cases CSV columns (required unless marked optional):
    id                 — unique snake_case identifier
    tier               — T1 | T2 | T3
    category           — one of categories.VALID_SLUGS
    domain             — free text (finance, credit, customer_analytics, ...)
    prompt             — natural-language question
    peak_sql           — golden SQL (multi-line OK)
    golden_reasoning   — what makes the answer correct (multi-line OK)
    tables             — optional, comma-separated
    sql_features       — optional, comma-separated
    ambiguity_flag     — Y | N
    evidence_required  — optional, Y | N (default N)
    status             — draft | approved (only `approved` rows are loaded)

Agent input CSV columns:
    id                 — must match a golden-case id
    agent_sql          — the agent's SQL
    agent_explanation  — the agent's natural-language explanation
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from categories import VALID_SLUGS, lookup as lookup_category

Tier = Literal["T1", "T2", "T3"]
VALID_TIERS: tuple[Tier, ...] = ("T1", "T2", "T3")
GOLDEN_REQUIRED_COLUMNS = (
    "id", "tier", "category", "domain", "prompt", "peak_sql",
    "golden_reasoning", "ambiguity_flag", "status",
)
AGENT_REQUIRED_COLUMNS = ("id", "agent_sql", "agent_explanation")


@dataclass
class GoldenCase:
    id: str
    tier: Tier
    category: str
    domain: str
    prompt: str
    peak_sql: str
    golden_reasoning: str
    ambiguity_flag: bool
    status: str
    tables: list[str]
    sql_features: list[str]
    evidence_required: bool


@dataclass
class AgentInput:
    id: str
    agent_sql: str
    agent_explanation: str


@dataclass
class PairedCase:
    golden: GoldenCase
    agent: AgentInput


def _yn(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in ("", "n", "no", "false", "0"):
        return False
    if v in ("y", "yes", "true", "1"):
        return True
    raise ValueError(f"expected Y/N, got {value!r}")


def _csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _check_columns(header: list[str], required: tuple[str, ...], path: Path) -> None:
    missing = [c for c in required if c not in header]
    if missing:
        raise ValueError(
            f"{path}: missing required columns {missing}. "
            f"Header found: {header}"
        )


def load_golden_cases(path: Path, *, only_approved: bool = True) -> list[GoldenCase]:
    """Load and validate golden_cases.csv.

    By default, returns only rows with status='approved' so draft cases don't
    accidentally affect benchmark scores. Pass only_approved=False to include
    drafts (useful while authoring).
    """
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty CSV (no header)")
        _check_columns(list(reader.fieldnames), GOLDEN_REQUIRED_COLUMNS, path)

        cases: list[GoldenCase] = []
        seen_ids: set[str] = set()
        for i, row in enumerate(reader, start=2):  # start=2 = first data row in file
            id_ = (row.get("id") or "").strip()
            if not id_:
                continue  # blank row — skip silently

            if id_ in seen_ids:
                raise ValueError(f"{path}:{i}: duplicate id '{id_}'")
            seen_ids.add(id_)

            tier = (row["tier"] or "").strip().upper()
            if tier not in VALID_TIERS:
                raise ValueError(
                    f"{path}:{i} ({id_}): tier must be one of {VALID_TIERS}, got {tier!r}"
                )

            category = (row["category"] or "").strip()
            if category not in VALID_SLUGS:
                raise ValueError(
                    f"{path}:{i} ({id_}): category must be one of "
                    f"{VALID_SLUGS}, got {category!r}"
                )

            status = (row["status"] or "").strip().lower()
            if only_approved and status != "approved":
                continue

            try:
                ambiguity = _yn(row["ambiguity_flag"])
                evidence = _yn(row.get("evidence_required"), default=False)
            except ValueError as e:
                raise ValueError(f"{path}:{i} ({id_}): {e}") from e

            cases.append(
                GoldenCase(
                    id=id_,
                    tier=tier,  # type: ignore[arg-type]
                    category=category,
                    domain=(row["domain"] or "").strip(),
                    prompt=row["prompt"].strip(),
                    peak_sql=row["peak_sql"],
                    golden_reasoning=(row["golden_reasoning"] or "").strip(),
                    ambiguity_flag=ambiguity,
                    status=status,
                    tables=_csv_list(row.get("tables")),
                    sql_features=_csv_list(row.get("sql_features")),
                    evidence_required=evidence,
                )
            )

    # Validate categories are real (also surfaces typos early)
    for case in cases:
        lookup_category(case.category)
    return cases


def load_agent_inputs(path: Path) -> list[AgentInput]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty CSV (no header)")
        _check_columns(list(reader.fieldnames), AGENT_REQUIRED_COLUMNS, path)

        rows: list[AgentInput] = []
        seen: set[str] = set()
        for i, row in enumerate(reader, start=2):
            id_ = (row.get("id") or "").strip()
            if not id_:
                continue
            if id_ in seen:
                raise ValueError(f"{path}:{i}: duplicate id '{id_}'")
            seen.add(id_)

            sql = (row.get("agent_sql") or "").strip()
            explanation = (row.get("agent_explanation") or "").strip()
            if not sql or not explanation:
                raise ValueError(
                    f"{path}:{i} ({id_}): agent_sql and agent_explanation are required"
                )
            rows.append(AgentInput(id=id_, agent_sql=sql, agent_explanation=explanation))
    return rows


def pair_cases(
    goldens: list[GoldenCase],
    inputs: list[AgentInput],
) -> tuple[list[PairedCase], list[str], list[str]]:
    """Pair golden cases to agent inputs by id.

    Returns (paired, unanswered_ids, extra_input_ids).
    """
    inputs_by_id = {a.id: a for a in inputs}
    golden_ids = {g.id for g in goldens}

    paired = [
        PairedCase(golden=g, agent=inputs_by_id[g.id])
        for g in goldens
        if g.id in inputs_by_id
    ]
    unanswered = [g.id for g in goldens if g.id not in inputs_by_id]
    extra = [a.id for a in inputs if a.id not in golden_ids]
    return paired, unanswered, extra
