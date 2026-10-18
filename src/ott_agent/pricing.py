"""Pricing config from the pack (config/pricing.json): the only plans and prices that exist (rules.md Rule 10.5)."""

from __future__ import annotations

import json
from pathlib import Path


def load_plan_days(path: Path) -> dict[str, int]:
    """plan_id -> plan length in days, e.g. {"P_TRIAL": 3, "P_MONTH": 30, "P_QUARTER": 90}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {p["plan_id"]: int(p["days"]) for p in data.get("plans", []) if p.get("plan_id") and p.get("days")}
