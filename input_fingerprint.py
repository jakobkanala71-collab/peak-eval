"""Stable fingerprint for a full benchmark run.

A run is uniquely identified by THREE inputs, not just the agent CSV:
  1. the agent submission (canonicalised — sorted rows, stripped fields),
  2. the golden corpus (raw bytes of the CSV),
  3. the judge prompt (SHA-256 hash, supplied by caller).

If any of the three changes, the run produces different (and informative)
scores — so we want to allow it. If all three are unchanged, the run would
be wasted spend, so we refuse and surface the prior report.

Earlier versions hashed only the agent CSV, which incorrectly blocked
re-runs after a judge-prompt or corpus edit.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

INDEX_FILENAME = "_input_index.json"


@dataclass
class PriorRun:
    fingerprint: str
    agent_name: str
    timestamp: str
    report_path: str
    source_csv: str
    agent_hash: str = ""
    corpus_hash: str = ""
    judge_prompt_hash: str = ""


def _agent_canonical_blob(agent_csv: Path) -> bytes:
    with agent_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    canon = sorted(
        (
            (
                (r.get("id") or "").strip(),
                (r.get("agent_sql") or "").strip(),
                (r.get("agent_explanation") or "").strip(),
            )
            for r in rows
        ),
        key=lambda x: x[0],
    )
    return "\n".join("\x1f".join(row) for row in canon).encode("utf-8")


def _sha16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def compute_fingerprint(
    agent_csv: Path,
    *,
    judge_prompt_hash: str,
    corpus_csv: Path,
) -> str:
    """Composite hash over (agent submission, corpus, judge prompt).

    Any change to any of the three flips the fingerprint, so the dedup
    layer only blocks a re-run when *nothing relevant* has changed.
    """
    agent = _agent_canonical_blob(agent_csv)
    corpus = corpus_csv.read_bytes()

    h = hashlib.sha256()
    h.update(b"agent:"); h.update(agent)
    h.update(b"\ncorpus:"); h.update(corpus)
    h.update(b"\njudge:"); h.update(judge_prompt_hash.encode("utf-8"))
    return h.hexdigest()[:16]


def component_hashes(
    agent_csv: Path,
    *,
    judge_prompt_hash: str,
    corpus_csv: Path,
) -> dict[str, str]:
    """Per-component hashes — useful for explaining *which* input changed."""
    return {
        "agent_hash": _sha16(_agent_canonical_blob(agent_csv)),
        "corpus_hash": _sha16(corpus_csv.read_bytes()),
        "judge_prompt_hash": judge_prompt_hash,
    }


def _index_path(benchmark_dir: Path) -> Path:
    return benchmark_dir / INDEX_FILENAME


def load_index(benchmark_dir: Path) -> dict:
    path = _index_path(benchmark_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def lookup(benchmark_dir: Path, fingerprint: str) -> PriorRun | None:
    entry = load_index(benchmark_dir).get(fingerprint)
    if not entry:
        return None
    return PriorRun(fingerprint=fingerprint, **entry)


def record(
    benchmark_dir: Path,
    *,
    fingerprint: str,
    agent_name: str,
    timestamp: str,
    report_path: str,
    source_csv: str,
    agent_hash: str = "",
    corpus_hash: str = "",
    judge_prompt_hash: str = "",
) -> None:
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    idx = load_index(benchmark_dir)
    idx[fingerprint] = {
        "agent_name": agent_name,
        "timestamp": timestamp,
        "report_path": report_path,
        "source_csv": source_csv,
        "agent_hash": agent_hash,
        "corpus_hash": corpus_hash,
        "judge_prompt_hash": judge_prompt_hash,
    }
    _index_path(benchmark_dir).write_text(json.dumps(idx, indent=2, sort_keys=True))
