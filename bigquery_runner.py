"""Execute SQL in BigQuery with cost + row guardrails.

Every query is dry-run first to estimate bytes scanned. Refuses to execute
if the estimate exceeds MAX_BYTES_SCANNED. After execution, refuses if the
result row count exceeds MAX_RESULT_ROWS — benchmark queries should return
small aggregates, not data dumps.

Read-only by design at three layers:
1. Code: only `SELECT`/`WITH` queries pass `assert_read_only`. Anything else
   raises NotReadOnly *before* the SQL leaves this process.
2. Architecture: no path here ever issues DDL/DML — we only call
   `client.query(sql)` with caller-supplied strings.
3. IAM (your responsibility): use a service account or ADC identity with
   `BigQuery Job User` + `BigQuery Data Viewer` roles only — no write/delete.
   This is the strongest guarantee; the code check is a backstop.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import pandas as pd
from google.cloud import bigquery

MAX_BYTES_SCANNED = 1 * 1024**3  # 1 GB per query
MAX_RESULT_ROWS = 10_000


@dataclass
class QueryStats:
    bytes_scanned: int
    duration_ms: float
    row_count: int


class QueryError(RuntimeError):
    """Base class for query failures (syntax, permission, timeout, etc.)."""


class BytesScannedExceeded(QueryError):
    """Dry-run estimated more bytes scanned than the configured ceiling."""


class ResultTooLarge(QueryError):
    """Query returned more rows than the configured cap."""


class NotReadOnly(QueryError):
    """SQL is not a read-only SELECT/WITH query."""


_SQL_COMMENT_RE = re.compile(r"(--[^\n]*)|(/\*.*?\*/)", re.DOTALL)


def assert_read_only(sql: str) -> None:
    """Refuse anything that isn't a SELECT/WITH query, before it reaches BigQuery.

    Strips SQL comments, drops a single trailing semicolon, then checks:
    - first non-whitespace token is SELECT or WITH (case-insensitive)
    - no second statement follows a semicolon (multi-statement scripts banned)

    Note: IAM read-only is the real safety; this function is a backstop that
    prevents an obviously-write query from even being attempted.
    """
    stripped = _SQL_COMMENT_RE.sub(" ", sql).strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if not stripped:
        raise NotReadOnly("empty SQL")

    if ";" in stripped:
        rest = stripped[stripped.index(";") + 1 :].strip()
        if rest:
            raise NotReadOnly("multi-statement queries not allowed")

    first = stripped.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        raise NotReadOnly(
            f"only SELECT/WITH queries are allowed; got '{first}'. "
            f"peak is read-only by design."
        )


def make_client() -> bigquery.Client:
    project = os.environ.get("BQ_PROJECT")
    if not project:
        raise RuntimeError("BQ_PROJECT not set in environment.")
    return bigquery.Client(project=project)


def execute(sql: str, client: bigquery.Client | None = None) -> tuple[pd.DataFrame, QueryStats]:
    """Run sql with dry-run cost check, real execution (cache off), and row cap.

    Returns the result DataFrame and execution stats.
    Raises BytesScannedExceeded, ResultTooLarge, or QueryError.
    """
    if client is None:
        client = make_client()

    # Read-only check before anything leaves this process.
    assert_read_only(sql)

    # Dry-run: cost check before any data scans.
    dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        dry = client.query(sql, job_config=dry_config)
    except Exception as e:
        raise QueryError(f"dry-run failed: {e}") from e

    if dry.total_bytes_processed and dry.total_bytes_processed > MAX_BYTES_SCANNED:
        raise BytesScannedExceeded(
            f"would scan {dry.total_bytes_processed / 1e9:.2f} GB — "
            f"exceeds {MAX_BYTES_SCANNED / 1e9:.0f} GB ceiling"
        )

    # Real run: cache off so timing is measured (not cache-hit micro-latency).
    real_config = bigquery.QueryJobConfig(use_query_cache=False)
    try:
        job = client.query(sql, job_config=real_config)
        df = job.to_dataframe()
    except Exception as e:
        raise QueryError(f"execution failed: {e}") from e

    if len(df) > MAX_RESULT_ROWS:
        raise ResultTooLarge(
            f"returned {len(df):,} rows — exceeds {MAX_RESULT_ROWS:,} row cap "
            f"(benchmark queries should be aggregations, not data dumps)"
        )

    duration_ms = 0.0
    if job.ended and job.started:
        duration_ms = (job.ended - job.started).total_seconds() * 1000

    return df, QueryStats(
        bytes_scanned=job.total_bytes_processed or 0,
        duration_ms=duration_ms,
        row_count=len(df),
    )
