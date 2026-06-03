"""Case categories and trust-risk classification.

The 12 categories from the peak_v2 framing. Each has:
- a short slug used in the corpus CSV
- a trust-risk level (low / medium / high)
- a flag indicating whether it's part of the trust-critical aggregate
- a flag indicating whether it's part of the calibration aggregate

When you change this file, update benchmark_cases.html and peak_v2.md to match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TrustRisk = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Category:
    slug: str
    title: str
    trust_risk: TrustRisk
    trust_critical: bool  # included in trust-critical aggregate
    calibration: bool  # included in calibration aggregate (uncertainty/refusal)


CATEGORIES: tuple[Category, ...] = (
    Category("translation_correctness", "Translation correctness",
             "low", False, False),
    Category("schema_selection", "Schema selection",
             "high", True, False),
    Category("business_logic", "Business logic & domain knowledge",
             "high", True, False),
    Category("temporal_logic", "Temporal & calendar logic",
             "high", True, False),
    Category("silent_wrongness", "Silent-wrongness traps",
             "high", True, False),
    Category("ambiguity_handling", "Ambiguity handling",
             "high", False, True),
    Category("hallucination_resistance", "Hallucination resistance",
             "high", True, False),
    Category("refusal_scope", "Refusal & scope awareness",
             "medium", False, True),
    Category("efficiency", "Efficiency / cost",
             "medium", False, False),
    Category("reasoning_faithfulness", "Reasoning faithfulness",
             "high", True, False),
    Category("consistency_rephrasing", "Consistency under rephrasing",
             "medium", False, False),
    Category("adversarial_robustness", "Adversarial / robustness",
             "low", False, False),
)

CATEGORY_BY_SLUG = {c.slug: c for c in CATEGORIES}
VALID_SLUGS = tuple(c.slug for c in CATEGORIES)
TRUST_CRITICAL_SLUGS = tuple(c.slug for c in CATEGORIES if c.trust_critical)
CALIBRATION_SLUGS = tuple(c.slug for c in CATEGORIES if c.calibration)


def lookup(slug: str) -> Category:
    if slug not in CATEGORY_BY_SLUG:
        raise ValueError(
            f"Unknown category '{slug}'. Valid categories: {', '.join(VALID_SLUGS)}"
        )
    return CATEGORY_BY_SLUG[slug]
