"""B1N-413 fee overlay for the published B1N-345 Wheel evidence.

The historical option outcomes are not reinterpreted as fills. This overlay
only applies the currently approved fee semantics to the already published,
modeled Wheel rows so the policy artifact cannot rely on obsolete fee inputs.
"""

from __future__ import annotations

import gzip
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


def apply_current_fees(
    row: dict[str, Any],
    *,
    protocol_premium_fee_bps: int = 1000,
    management_fee_bps_annual: int = 200,
    performance_fee_bps: int = 1000,
) -> dict[str, float]:
    """Apply a conservative terminal fee overlay to one no-flow research row.

    Management fee uses the greater of opening and pre-parent-fee terminal NAV.
    Performance fee applies only to gains remaining above the opening HWM after
    premium and management fees. This is intentionally conservative and does
    not claim to replay share dilution or intra-window subscriptions.
    """
    initial = float(row["initial_usdc"])
    final_before_fees = float(row["final_nav_usdc"])
    gross_premium = float(row.get("premium_gross_usdc", 0.0))
    window_days = int(row["window_days"])
    premium_fee = gross_premium * protocol_premium_fee_bps / 10_000
    after_premium = final_before_fees - premium_fee
    management_base = max(initial, after_premium, 0.0)
    management_fee = (
        management_base * management_fee_bps_annual / 10_000 * window_days / 365
    )
    pre_performance = after_premium - management_fee
    performance_fee = max(pre_performance - initial, 0.0) * performance_fee_bps / 10_000
    final_after_fees = pre_performance - performance_fee
    return {
        "protocol_premium_fee_usdc": premium_fee,
        "parent_management_fee_usdc": management_fee,
        "parent_performance_fee_usdc": performance_fee,
        "final_nav_after_current_fees_usdc": final_after_fees,
        "return_after_current_fees": final_after_fees / initial - 1,
    }


def load_published_wheel_rows(path: Path) -> Iterable[dict[str, Any]]:
    with gzip.open(path, "rt") as source:
        for line in source:
            row = json.loads(line)
            if row.get("asset") == "ETH" and row.get("strategy") == "wheel":
                yield row


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def build_fee_adjusted_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[int, float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(int(row["window_days"]), float(row["target_delta"]))].append(row)

    policies = []
    for (window_days, target_delta), group in sorted(groups.items()):
        adjusted = [apply_current_fees(row) for row in group]
        returns = [item["return_after_current_fees"] for item in adjusted]
        policies.append(
            {
                "window_days": window_days,
                "target_delta": target_delta,
                "sample_count": len(group),
                "return_after_current_fees": {
                    "p5": _quantile(returns, 0.05),
                    "median": _quantile(returns, 0.50),
                    "p95": _quantile(returns, 0.95),
                    "worst": min(returns),
                },
                "loss_probability": sum(value < 0 for value in returns) / len(returns),
                "mean_protocol_premium_fee_usdc": statistics.fmean(
                    item["protocol_premium_fee_usdc"] for item in adjusted
                ),
                "mean_parent_management_fee_usdc": statistics.fmean(
                    item["parent_management_fee_usdc"] for item in adjusted
                ),
                "mean_parent_performance_fee_usdc": statistics.fmean(
                    item["parent_performance_fee_usdc"] for item in adjusted
                ),
                "production_ready": False,
            }
        )
    return {
        "authority_issue": "B1N-413",
        "source_issue": "B1N-345",
        "asset": "ETH",
        "observation_class": "modeled_fee_overlay_on_observed_spot_iv",
        "fee_method": (
            "10% gross premium; 2% annual management on max(opening, terminal); "
            "10% terminal gain above opening HWM after prior fees"
        ),
        "production_ready_fixed_policies": 0,
        "mainnet_authorized": False,
        "policies": policies,
    }
