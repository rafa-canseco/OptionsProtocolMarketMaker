from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

DERIBIT_BASE_URL = "https://www.deribit.com/api/v2/public"


def utc_timestamp_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def last_completed_deribit_expiry(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current.replace(hour=8, minute=0, second=0, microsecond=0)
    if current < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


class DeribitClient:
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()

    def get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(
            f"{DERIBIT_BASE_URL}/{method}",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"Deribit {method}: {payload['error']}")
        return payload


def extract_market_snapshot(
    output_dir: Path,
    cutoff: datetime,
    lookback_days: int = 180,
    client: DeribitClient | None = None,
) -> Path:
    """Download immutable spot/DVOL inputs used to replay Binary pricing."""
    api = client or DeribitClient()
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    cutoff_ms = utc_timestamp_ms(cutoff)
    start = cutoff - timedelta(days=lookback_days, hours=12)
    start_ms = utc_timestamp_ms(start)

    spot_params = {"index_name": "eth_usd", "range": "1y"}
    spot_payload = api.get("get_index_chart_data", spot_params)
    (raw_dir / "spot_index_1y.json").write_text(
        json.dumps(spot_payload, indent=2, sort_keys=True) + "\n"
    )

    dvol_pages: list[tuple[dict[str, Any], dict[str, Any]]] = []
    end_ms = cutoff_ms
    while True:
        params = {
            "currency": "ETH",
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
            "resolution": "3600",
        }
        payload = api.get("get_volatility_index_data", params)
        dvol_pages.append((params, payload))
        data = payload.get("result", {}).get("data", [])
        continuation = payload.get("result", {}).get("continuation")
        if not data or continuation is None or int(data[0][0]) <= start_ms:
            break
        next_end = int(continuation)
        if next_end >= end_ms:
            raise RuntimeError("Deribit DVOL pagination did not move backwards")
        end_ms = next_end

    for index, (_, payload) in enumerate(dvol_pages):
        (raw_dir / f"dvol_{index:03d}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )

    spot_rows = [
        {"timestamp_ms": int(row[0]), "value": float(row[1]), "source": "observed"}
        for row in spot_payload["result"]
        if start_ms <= int(row[0]) <= cutoff_ms
    ]
    dvol_by_timestamp: dict[int, dict[str, Any]] = {}
    for _, payload in dvol_pages:
        for row in payload["result"]["data"]:
            timestamp = int(row[0])
            if start_ms <= timestamp <= cutoff_ms:
                dvol_by_timestamp[timestamp] = {
                    "timestamp_ms": timestamp,
                    "value": float(row[4]) / 100.0,
                    "source": "observed",
                }
    iv_rows = [dvol_by_timestamp[key] for key in sorted(dvol_by_timestamp)]

    normalized = {
        "schema_version": 1,
        "cutoff": cutoff.isoformat(),
        "start": start.isoformat(),
        "normalization": {
            "spot": "Deribit ETH/USD index in USD per ETH",
            "iv": "Deribit ETH DVOL close divided by 100; annualized decimal",
            "premium": "not sourced from Deribit; replayed by Binary pricer",
            "payoff": "linear ETH/USDC, premium USD per ETH",
        },
        "spot": spot_rows,
        "iv": iv_rows,
    }
    market_path = output_dir / "market.json"
    market_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "cutoff": cutoff.isoformat(),
        "start": start.isoformat(),
        "playbook_context_gap": "playbook/CONTEXT.md absent in staging checkout",
        "sources": [
            {
                "method": "get_index_chart_data",
                "params": spot_params,
                "sha256": _sha256(spot_payload),
                "rows": len(spot_rows),
            },
            *[
                {
                    "method": "get_volatility_index_data",
                    "params": params,
                    "sha256": _sha256(payload),
                    "rows": len(payload["result"]["data"]),
                }
                for params, payload in dvol_pages
            ],
        ],
        "normalized": {
            "path": market_path.name,
            "sha256": hashlib.sha256(market_path.read_bytes()).hexdigest(),
            "spot_rows": len(spot_rows),
            "iv_rows": len(iv_rows),
        },
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return market_path


@dataclass(frozen=True)
class CausalValue:
    value: float
    timestamp_ms: int
    age_hours: float
    source: str


class MarketSeries:
    def __init__(self, market_path: Path) -> None:
        payload = json.loads(market_path.read_text())
        self.cutoff = datetime.fromisoformat(payload["cutoff"])
        self._spot = sorted(payload["spot"], key=lambda row: row["timestamp_ms"])
        self._iv = sorted(payload["iv"], key=lambda row: row["timestamp_ms"])
        self._spot_ts = [int(row["timestamp_ms"]) for row in self._spot]
        self._iv_ts = [int(row["timestamp_ms"]) for row in self._iv]

    @staticmethod
    def _at(
        rows: list[dict[str, Any]],
        timestamps: list[int],
        timestamp_ms: int,
        maximum_age_hours: float,
    ) -> CausalValue | None:
        index = bisect.bisect_right(timestamps, timestamp_ms) - 1
        if index < 0:
            return None
        row = rows[index]
        age_hours = (timestamp_ms - int(row["timestamp_ms"])) / 3_600_000
        if age_hours > maximum_age_hours:
            return None
        return CausalValue(
            value=float(row["value"]),
            timestamp_ms=int(row["timestamp_ms"]),
            age_hours=age_hours,
            source=str(row["source"]),
        )

    def spot_at(
        self, timestamp_ms: int, maximum_age_hours: float
    ) -> CausalValue | None:
        return self._at(self._spot, self._spot_ts, timestamp_ms, maximum_age_hours)

    def iv_at(self, timestamp_ms: int, maximum_age_hours: float) -> CausalValue | None:
        return self._at(self._iv, self._iv_ts, timestamp_ms, maximum_age_hours)
