#!/usr/bin/env python3
"""Build MACROSCOPE's static release calendar and official-data snapshot.

Designed for GitHub Actions + GitHub Pages. The script keeps the browser free of
API credentials by using FRED_API_KEY only inside the Actions runner, then writes
one public JSON artifact consumed by index.html.

Phase C adds:
- transparent pre-release forecasts for selected release families;
- standardized surprises using rolling historical forecast errors;
- a versioned, decaying release-shock ledger with revision replacement;
- forecast-performance statistics and event-level impact metadata.

Phase B foundation:
- mapped official observations for core U.S. macro series;
- current and previous-period values;
- initial-release comparisons where FRED/ALFRED supports them;
- direct model-input mappings and output anchors;
- partial-update fallback so one unavailable series does not break deployment.

Usage:
    FRED_API_KEY=your_key python scripts/update_calendar.py

Optional environment variables:
    CALENDAR_HISTORY_DAYS=365
    CALENDAR_FUTURE_DAYS=180
    CALENDAR_OUTPUT=data/economic-calendar.json
    FRED_TIMEOUT_SECONDS=120
    FRED_MAX_RETRIES=6
    FRED_CHUNK_DAYS=60
    ALLOW_STALE_CALENDAR=true
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FRED_API_ROOT = "https://api.stlouisfed.org/fred"

MAJOR_RELEASE_PATTERNS: tuple[str, ...] = (
    "employment situation",
    "consumer price index",
    "producer price index",
    "personal income and outlays",
    "gross domestic product",
    "retail sales",
    "industrial production",
    "capacity utilization",
    "durable goods",
    "manufacturers' shipments",
    "manufacturing and trade inventories",
    "job openings and labor turnover",
    "weekly claims",
    "unemployment insurance weekly claims",
    "housing starts",
    "new residential construction",
    "new residential sales",
    "existing home sales",
    "construction spending",
    "international trade",
    "factory orders",
    "productivity and costs",
    "federal open market committee",
    "fomc",
    "minutes of the federal open market committee",
    "monthly treasury statement",
    "advance monthly sales",
    "business formation statistics",
    "consumer credit",
    "beige book",
)

INCLUDE_RELEASE_IDS: set[int] = set()
EXCLUDE_RELEASE_IDS: set[int] = set()

# Registry values are intentionally explicit. They control both the updater and
# the browser-side provenance display, and avoid depending on fragile title
# matching or undocumented third-party calendar feeds.
SERIES_REGISTRY: dict[str, dict[str, Any]] = {
    "realGdpGrowth": {
        "seriesId": "A191RL1Q225SBEA",
        "label": "Real GDP Growth",
        "category": "Growth",
        "units": "% SAAR",
        "fredUnits": "lin",
        "precision": 1,
        "frequency": "Quarterly",
        "outputTarget": "gdp",
        "staleAfterDays": 160,
        "trackRevision": True,
        "releasePatterns": ("gross domestic product",),
    },
    "headlineCpiYoy": {
        "seriesId": "CPIAUCSL",
        "label": "Headline CPI",
        "category": "Inflation",
        "units": "% YoY",
        "fredUnits": "pc1",
        "precision": 1,
        "frequency": "Monthly",
        "outputTarget": "cpi",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("consumer price index",),
    },
    "coreCpiYoy": {
        "seriesId": "CPILFESL",
        "label": "Core CPI",
        "category": "Inflation",
        "units": "% YoY",
        "fredUnits": "pc1",
        "precision": 1,
        "frequency": "Monthly",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("consumer price index",),
    },
    "unemployment": {
        "seriesId": "UNRATE",
        "label": "Unemployment Rate",
        "category": "Labor",
        "units": "%",
        "fredUnits": "lin",
        "precision": 1,
        "frequency": "Monthly",
        "inputTarget": "unemp",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("employment situation",),
    },
    "laborForceParticipation": {
        "seriesId": "CIVPART",
        "label": "Labor Force Participation",
        "category": "Labor",
        "units": "%",
        "fredUnits": "lin",
        "precision": 1,
        "frequency": "Monthly",
        "inputTarget": "lfp",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("employment situation",),
    },
    "payrollChange": {
        "seriesId": "PAYEMS",
        "label": "Nonfarm Payroll Change",
        "category": "Labor",
        "units": "Thousands",
        "fredUnits": "chg",
        "precision": 0,
        "frequency": "Monthly",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("employment situation",),
    },
    "effectiveFedFunds": {
        "seriesId": "DFF",
        "label": "Effective Federal Funds Rate",
        "category": "Rates",
        "units": "%",
        "fredUnits": "lin",
        "precision": 2,
        "frequency": "Daily",
        "inputTarget": "ffr",
        "staleAfterDays": 8,
        "trackRevision": False,
        "releasePatterns": ("federal open market committee", "fomc"),
    },
    "interestOnReserveBalances": {
        "seriesId": "IORB",
        "label": "Interest on Reserve Balances",
        "category": "Rates",
        "units": "%",
        "fredUnits": "lin",
        "precision": 2,
        "frequency": "Daily",
        "inputTarget": "ioer",
        "staleAfterDays": 8,
        "trackRevision": False,
        "releasePatterns": ("federal open market committee", "fomc"),
    },
    "treasury2Year": {
        "seriesId": "DGS2",
        "label": "2-Year Treasury Yield",
        "category": "Rates",
        "units": "%",
        "fredUnits": "lin",
        "precision": 2,
        "frequency": "Daily",
        "outputTarget": "twoyr",
        "staleAfterDays": 8,
        "trackRevision": False,
        "releasePatterns": (),
    },
    "treasury10Year": {
        "seriesId": "DGS10",
        "label": "10-Year Treasury Yield",
        "category": "Rates",
        "units": "%",
        "fredUnits": "lin",
        "precision": 2,
        "frequency": "Daily",
        "outputTarget": "tenyr",
        "staleAfterDays": 8,
        "trackRevision": False,
        "releasePatterns": (),
    },
    "mortgage30Year": {
        "seriesId": "MORTGAGE30US",
        "label": "30-Year Fixed Mortgage Rate",
        "category": "Housing",
        "units": "%",
        "fredUnits": "lin",
        "precision": 2,
        "frequency": "Weekly",
        "outputTarget": "mortg",
        "staleAfterDays": 16,
        "trackRevision": False,
        "releasePatterns": (),
    },
    "housingStarts": {
        "seriesId": "HOUST",
        "label": "Housing Starts",
        "category": "Housing",
        "units": "Million SAAR",
        "fredUnits": "lin",
        "scale": 0.001,
        "precision": 2,
        "frequency": "Monthly",
        "inputTarget": "housingSupply",
        "staleAfterDays": 85,
        "trackRevision": True,
        "releasePatterns": ("housing starts", "new residential construction"),
    },
    "wtiOil": {
        "seriesId": "DCOILWTICO",
        "label": "WTI Crude Oil",
        "category": "Energy",
        "units": "$/bbl",
        "fredUnits": "lin",
        "precision": 2,
        "frequency": "Daily",
        "inputTarget": "oil",
        "staleAfterDays": 12,
        "trackRevision": False,
        "releasePatterns": (),
    },
    "pceInflationYoy": {
        "seriesId": "PCEPI",
        "label": "PCE Price Inflation",
        "category": "Inflation",
        "units": "% YoY",
        "fredUnits": "pc1",
        "precision": 1,
        "frequency": "Monthly",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("personal income and outlays",),
    },
    "retailSalesMmom": {
        "seriesId": "RSAFS",
        "label": "Advance Retail Sales",
        "category": "Consumption",
        "units": "% MoM",
        "fredUnits": "pch",
        "precision": 1,
        "frequency": "Monthly",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("retail sales", "advance monthly sales"),
    },
    "realPceGrowth": {
        "seriesId": "DPCERAM1M225NBEA",
        "label": "Real Personal Consumption Growth",
        "category": "Consumption",
        "units": "% annualized",
        "fredUnits": "lin",
        "precision": 1,
        "frequency": "Monthly",
        "staleAfterDays": 75,
        "trackRevision": True,
        "releasePatterns": ("personal income and outlays",),
    },
}

# A +1.0 standardized surprise is translated through these explicit channels.
# These are transparent expert-prior coefficients, not statistically estimated
# causal effects. The generated JSON exposes every coefficient and forecast.
SURPRISE_SERIES_SPECS: dict[str, dict[str, Any]] = {
    "payrollChange": {
        "family": "Employment Situation", "minErrorScale": 45.0,
        "durationMonths": 4, "peakMonth": 0, "decayRate": 0.42, "confidence": 0.78,
        "channels": {"gdpA": 0.12, "ffr": 0.06, "guidanceTone": 0.25, "confA": 1.5, "recessionRiskA": -2.5, "spA": 1.8},
    },
    "unemployment": {
        "family": "Employment Situation", "minErrorScale": 0.12,
        "durationMonths": 5, "peakMonth": 0, "decayRate": 0.36, "confidence": 0.82,
        "channels": {"gdpA": -0.10, "ffr": -0.05, "guidanceTone": -0.22, "confA": -1.4, "recessionRiskA": 3.0, "spA": -1.4},
    },
    "headlineCpiYoy": {
        "family": "Consumer Price Index", "minErrorScale": 0.12,
        "durationMonths": 4, "peakMonth": 0, "decayRate": 0.40, "confidence": 0.84,
        "channels": {"cpiA": 0.16, "ffr": 0.05, "guidanceTone": 0.24, "twoyrA": 0.09, "tenyrA": 0.05, "mortgA": 0.04, "spA": -1.0},
    },
    "coreCpiYoy": {
        "family": "Consumer Price Index", "minErrorScale": 0.10,
        "durationMonths": 5, "peakMonth": 0, "decayRate": 0.34, "confidence": 0.88,
        "channels": {"cpiA": 0.20, "ffr": 0.07, "guidanceTone": 0.30, "twoyrA": 0.12, "tenyrA": 0.06, "mortgA": 0.05, "spA": -1.3},
    },
    "realGdpGrowth": {
        "family": "Gross Domestic Product", "minErrorScale": 0.55,
        "durationMonths": 6, "peakMonth": 0, "decayRate": 0.28, "confidence": 0.76,
        "channels": {"gdpA": 0.18, "ffr": 0.04, "guidanceTone": 0.15, "confA": 1.0, "recessionRiskA": -2.0, "spA": 1.2},
    },
    "retailSalesMmom": {
        "family": "Retail Sales", "minErrorScale": 0.65,
        "durationMonths": 3, "peakMonth": 0, "decayRate": 0.52, "confidence": 0.72,
        "channels": {"gdpA": 0.12, "cpiA": 0.02, "ffr": 0.02, "confA": 0.9, "recessionRiskA": -1.2, "spA": 0.8},
    },
    "pceInflationYoy": {
        "family": "Personal Income and Outlays", "minErrorScale": 0.10,
        "durationMonths": 5, "peakMonth": 0, "decayRate": 0.34, "confidence": 0.86,
        "channels": {"cpiA": 0.18, "ffr": 0.06, "guidanceTone": 0.26, "twoyrA": 0.10, "tenyrA": 0.05, "mortgA": 0.04, "spA": -1.0},
    },
    "realPceGrowth": {
        "family": "Personal Income and Outlays", "minErrorScale": 1.2,
        "durationMonths": 3, "peakMonth": 0, "decayRate": 0.50, "confidence": 0.70,
        "channels": {"gdpA": 0.10, "confA": 0.8, "recessionRiskA": -1.0, "spA": 0.6},
    },
}



@dataclass(frozen=True)
class Config:
    api_key: str
    history_days: int
    future_days: int
    output: Path
    timeout_seconds: int
    max_retries: int
    chunk_days: int
    allow_stale: bool


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer; received {raw!r}") from exc
    return max(minimum, min(maximum, value))


def load_config() -> Config:
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "FRED_API_KEY is not set. Add it as a GitHub Actions repository "
            "secret or export it before running this script."
        )
    return Config(
        api_key=api_key,
        history_days=env_int("CALENDAR_HISTORY_DAYS", 365, 1, 3650),
        future_days=env_int("CALENDAR_FUTURE_DAYS", 180, 1, 730),
        output=Path(os.environ.get("CALENDAR_OUTPUT", "data/economic-calendar.json")),
        timeout_seconds=env_int("FRED_TIMEOUT_SECONDS", 120, 15, 300),
        max_retries=env_int("FRED_MAX_RETRIES", 6, 1, 10),
        chunk_days=env_int("FRED_CHUNK_DAYS", 60, 7, 180),
        allow_stale=env_bool("ALLOW_STALE_CALENDAR", True),
    )


def retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(120.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(45.0, 2.0**attempt) + random.uniform(0.2, 1.2)


def fred_get(
    endpoint: str,
    params: dict[str, Any],
    config: Config,
    *,
    max_retries: int | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    attempts = max_retries or config.max_retries
    timeout = timeout_seconds or config.timeout_seconds
    query = urlencode(params)
    url = f"{FRED_API_ROOT}/{endpoint}?{query}"
    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "MACROSCOPE-official-data/3.0",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise RuntimeError("Unexpected FRED response: root value is not an object")
            return payload
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            transient = exc.code == 429 or 500 <= exc.code < 600
            if transient and attempt < attempts:
                delay = retry_delay(attempt, exc.headers.get("Retry-After"))
                print(
                    f"FRED {endpoint} HTTP {exc.code}; retry {attempt}/{attempts} "
                    f"in {delay:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"FRED {endpoint} HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt < attempts:
                delay = retry_delay(attempt)
                print(
                    f"FRED {endpoint} network error ({exc}); retry "
                    f"{attempt}/{attempts} in {delay:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"Could not reach FRED {endpoint} after {attempts} attempts: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            if attempt < attempts:
                delay = retry_delay(attempt)
                print(
                    f"FRED {endpoint} returned invalid JSON; retry "
                    f"{attempt}/{attempts} in {delay:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"FRED {endpoint} returned invalid JSON after repeated attempts"
            ) from exc
    raise RuntimeError(f"FRED {endpoint} request failed after retries")


def read_existing_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def is_major_release(item: dict[str, Any]) -> bool:
    try:
        release_id = int(item.get("release_id", 0))
    except (TypeError, ValueError):
        release_id = 0
    if release_id in EXCLUDE_RELEASE_IDS:
        return False
    if release_id in INCLUDE_RELEASE_IDS:
        return True
    name = str(item.get("release_name", "")).casefold()
    return any(pattern in name for pattern in MAJOR_RELEASE_PATTERNS)


def date_windows(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=days - 1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def collect_window(config: Config, start: date, end: date) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    offset = 0
    limit = 1000
    while True:
        payload = fred_get(
            "releases/dates",
            {
                "api_key": config.api_key,
                "file_type": "json",
                "realtime_start": start.isoformat(),
                "realtime_end": end.isoformat(),
                "include_release_dates_with_no_data": "true",
                "limit": limit,
                "offset": offset,
                "sort_order": "asc",
            },
            config,
        )
        batch = payload.get("release_dates", [])
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected FRED response: release_dates is not a list")
        events.extend(item for item in batch if isinstance(item, dict) and is_major_release(item))
        try:
            count = int(payload.get("count", len(batch)))
            returned_offset = int(payload.get("offset", offset))
            returned_limit = int(payload.get("limit", limit))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Unexpected FRED pagination metadata") from exc
        next_offset = returned_offset + returned_limit
        if next_offset >= count or not batch:
            break
        offset = next_offset
    return events


def collect_release_dates(config: Config, start: date, end: date) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    windows = list(date_windows(start, end, config.chunk_days))
    for index, (window_start, window_end) in enumerate(windows, start=1):
        print(
            f"Downloading FRED calendar window {index}/{len(windows)}: "
            f"{window_start} through {window_end}"
        )
        events.extend(collect_window(config, window_start, window_end))
    return events


def normalize_events(raw_events: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for item in raw_events:
        raw_date = str(item.get("date", ""))
        try:
            event_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        try:
            release_id = int(item.get("release_id", 0))
        except (TypeError, ValueError):
            release_id = 0
        name = str(item.get("release_name", "")).strip()
        if not name:
            continue
        status = "released" if event_date < today else "today" if event_date == today else "scheduled"
        unique[(raw_date, release_id)] = {
            "id": f"fred-{release_id}-{raw_date}",
            "releaseId": release_id,
            "name": name,
            "date": raw_date,
            "time": None,
            "timezone": "America/New_York",
            "source": "FRED",
            "status": status,
            "importance": "major",
        }
    return sorted(unique.values(), key=lambda event: (event["date"], event["name"]))


def parse_valid_observations(payload: dict[str, Any], scale: float) -> list[dict[str, Any]]:
    raw = payload.get("observations", [])
    if not isinstance(raw, list):
        raise RuntimeError("Unexpected FRED response: observations is not a list")
    parsed: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        value_raw = item.get("value")
        if value_raw in (None, ".", ""):
            continue
        try:
            value = float(value_raw) * scale
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        parsed.append(
            {
                "date": str(item.get("date", "")),
                "value": value,
                "realtimeStart": item.get("realtime_start"),
                "realtimeEnd": item.get("realtime_end"),
            }
        )
    return parsed


def weighted_three_period_forecast(prior_values: list[float]) -> float | None:
    """Transparent one-step forecast using the three most recent prior values."""
    if not prior_values:
        return None
    weights = (0.55, 0.30, 0.15)
    usable = prior_values[:3]
    active_weights = weights[: len(usable)]
    total = sum(active_weights)
    return sum(value * weight for value, weight in zip(usable, active_weights)) / total


def forecast_diagnostics(
    key: str,
    rows_desc: list[dict[str, Any]],
    precision: int,
) -> dict[str, Any] | None:
    spec = SURPRISE_SERIES_SPECS.get(key)
    if not spec or len(rows_desc) < 3:
        return None
    prior = [float(row["value"]) for row in rows_desc[1:4]]
    expected = weighted_three_period_forecast(prior)
    if expected is None:
        return None
    actual = float(rows_desc[0]["value"])
    errors: list[float] = []
    direction_hits = 0
    direction_total = 0
    max_backtests = min(24, max(0, len(rows_desc) - 3))
    # rows are descending; row j is forecast from j+1, j+2, j+3.
    for j in range(1, max_backtests + 1):
        if j + 2 >= len(rows_desc):
            break
        hist_prior = [float(row["value"]) for row in rows_desc[j + 1 : j + 4]]
        forecast = weighted_three_period_forecast(hist_prior)
        if forecast is None:
            continue
        observed = float(rows_desc[j]["value"])
        errors.append(observed - forecast)
        previous = float(rows_desc[j + 1]["value"])
        pred_dir = 0 if abs(forecast - previous) < 1e-12 else (1 if forecast > previous else -1)
        act_dir = 0 if abs(observed - previous) < 1e-12 else (1 if observed > previous else -1)
        direction_hits += int(pred_dir == act_dir)
        direction_total += 1
    min_scale = float(spec.get("minErrorScale", 0.1))
    if errors:
        mae = sum(abs(error) for error in errors) / len(errors)
        rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    else:
        mae = min_scale
        rmse = min_scale
    error_scale = max(min_scale, rmse)
    surprise = actual - expected
    surprise_z = max(-3.0, min(3.0, surprise / error_scale if error_scale else 0.0))
    next_expected = weighted_three_period_forecast([actual] + prior[:2])
    return {
        "method": "Weighted three-period trend (55/30/15)",
        "expected": round(expected, precision + 3),
        "nextExpected": round(next_expected, precision + 3) if next_expected is not None else None,
        "surprise": round(surprise, precision + 3),
        "errorScale": round(error_scale, precision + 3),
        "surpriseZ": round(surprise_z, 3),
        "performance": {
            "backtestCount": len(errors),
            "mae": round(mae, precision + 3),
            "rmse": round(rmse, precision + 3),
            "directionalAccuracy": round(direction_hits / direction_total, 3) if direction_total else None,
        },
    }


def fetch_initial_release(
    config: Config,
    spec: dict[str, Any],
    observation_date: str,
) -> float | None:
    if not spec.get("trackRevision"):
        return None
    params = {
        "api_key": config.api_key,
        "file_type": "json",
        "series_id": spec["seriesId"],
        "observation_start": observation_date,
        "observation_end": observation_date,
        "sort_order": "desc",
        "limit": 10,
        "units": spec.get("fredUnits", "lin"),
        "output_type": 4,
    }
    try:
        payload = fred_get("series/observations", params, config, max_retries=min(2, config.max_retries), timeout_seconds=min(45, config.timeout_seconds))
        rows = parse_valid_observations(payload, float(spec.get("scale", 1.0)))
        return rows[0]["value"] if rows else None
    except Exception as exc:
        print(
            f"::warning::Initial-release lookup failed for {spec['seriesId']}: {exc}",
            file=sys.stderr,
        )
        return None


def fetch_official_observation(
    key: str,
    spec: dict[str, Any],
    config: Config,
    today: date,
) -> dict[str, Any]:
    lookback_days = 1400 if spec.get("frequency") == "Quarterly" else 800
    payload = fred_get(
        "series/observations",
        {
            "api_key": config.api_key,
            "file_type": "json",
            "series_id": spec["seriesId"],
            "observation_start": (today - timedelta(days=lookback_days)).isoformat(),
            "observation_end": today.isoformat(),
            "sort_order": "desc",
            "limit": 50,
            "units": spec.get("fredUnits", "lin"),
        },
        config,
        max_retries=min(3, config.max_retries),
        timeout_seconds=min(60, config.timeout_seconds),
    )
    rows = parse_valid_observations(payload, float(spec.get("scale", 1.0)))
    if not rows:
        raise RuntimeError(f"No usable observations returned for {spec['seriesId']}")
    current = rows[0]
    previous = rows[1] if len(rows) > 1 else None
    initial_release = fetch_initial_release(config, spec, current["date"])
    revision = None
    if initial_release is not None:
        revision = current["value"] - initial_release
    observation_date = date.fromisoformat(current["date"])
    age_days = max(0, (today - observation_date).days)
    precision = int(spec.get("precision", 2))
    forecast = forecast_diagnostics(key, rows, precision)

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, precision + 2)

    return {
        "key": key,
        "seriesId": spec["seriesId"],
        "label": spec["label"],
        "category": spec["category"],
        "frequency": spec["frequency"],
        "units": spec["units"],
        "precision": precision,
        "observationDate": current["date"],
        "actual": rounded(current["value"]),
        "previous": rounded(previous["value"] if previous else None),
        "previousDate": previous["date"] if previous else None,
        "change": rounded(current["value"] - previous["value"] if previous else None),
        "initialRelease": rounded(initial_release),
        "revision": rounded(revision),
        "inputTarget": spec.get("inputTarget"),
        "outputTarget": spec.get("outputTarget"),
        "releasePatterns": list(spec.get("releasePatterns", ())),
        "ageDays": age_days,
        "staleAfterDays": int(spec.get("staleAfterDays", 45)),
        "isStale": age_days > int(spec.get("staleAfterDays", 45)),
        "source": "FRED",
        "retrievedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "forecast": forecast,
    }


def collect_official_data(
    config: Config,
    today: date,
    previous_payload: dict[str, Any],
) -> dict[str, Any]:
    previous_observations = (
        previous_payload.get("officialData", {}).get("observations", {})
        if isinstance(previous_payload.get("officialData"), dict)
        else {}
    )
    observations: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    stale_fallbacks: list[str] = []

    for index, (key, spec) in enumerate(SERIES_REGISTRY.items(), start=1):
        print(
            f"Downloading official series {index}/{len(SERIES_REGISTRY)}: "
            f"{spec['seriesId']} ({spec['label']})"
        )
        try:
            observations[key] = fetch_official_observation(key, spec, config, today)
        except Exception as exc:
            old = previous_observations.get(key) if isinstance(previous_observations, dict) else None
            if isinstance(old, dict) and old.get("actual") is not None:
                fallback = dict(old)
                fallback["fallback"] = True
                fallback["fallbackReason"] = str(exc)
                observations[key] = fallback
                stale_fallbacks.append(key)
                print(
                    f"::warning::Using prior snapshot for {spec['seriesId']}: {exc}",
                    file=sys.stderr,
                )
            else:
                errors.append({"key": key, "seriesId": spec["seriesId"], "message": str(exc)})
                print(
                    f"::warning::Official series unavailable {spec['seriesId']}: {exc}",
                    file=sys.stderr,
                )

    baseline_inputs: dict[str, float] = {}
    baseline_outputs: dict[str, float] = {}
    for observation in observations.values():
        actual = observation.get("actual")
        if not isinstance(actual, (int, float)) or not math.isfinite(actual):
            continue
        if observation.get("inputTarget"):
            baseline_inputs[str(observation["inputTarget"])] = actual
        if observation.get("outputTarget"):
            baseline_outputs[str(observation["outputTarget"])] = actual

    status = "complete"
    if errors or stale_fallbacks:
        status = "partial"
    if not observations:
        status = "unavailable"

    return {
        "schemaVersion": 1,
        "country": "us",
        "status": status,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seriesCount": len(observations),
        "freshSeriesCount": len(observations) - len(stale_fallbacks),
        "staleFallbackCount": len(stale_fallbacks),
        "baseline": {
            "inputs": baseline_inputs,
            "outputs": baseline_outputs,
        },
        "observations": observations,
        "errors": errors,
        "staleFallbacks": stale_fallbacks,
    }


def attach_data_to_events(
    events: list[dict[str, Any]],
    official_data: dict[str, Any],
    today: date,
) -> list[dict[str, Any]]:
    observations = official_data.get("observations", {})
    if not isinstance(observations, dict):
        return events

    # Add the relevant data keys to every matching release. Attach actual values
    # only to the latest released occurrence of that release family.
    latest_match: dict[str, tuple[str, int]] = {}
    for index, event in enumerate(events):
        name = str(event.get("name", "")).casefold()
        data_keys: list[str] = []
        for key, observation in observations.items():
            patterns = observation.get("releasePatterns", [])
            if any(str(pattern).casefold() in name for pattern in patterns):
                data_keys.append(key)
                if event.get("date", "") <= today.isoformat():
                    previous = latest_match.get(key)
                    if previous is None or event["date"] > previous[0]:
                        latest_match[key] = (event["date"], index)
        if data_keys:
            event["dataKeys"] = data_keys

    by_event: dict[int, list[str]] = {}
    for key, (_, event_index) in latest_match.items():
        by_event.setdefault(event_index, []).append(key)

    for event_index, keys in by_event.items():
        summaries = []
        for key in keys:
            obs = observations.get(key, {})
            if obs.get("actual") is None:
                continue
            summaries.append(
                {
                    "key": key,
                    "label": obs.get("label"),
                    "actual": obs.get("actual"),
                    "previous": obs.get("previous"),
                    "revision": obs.get("revision"),
                    "units": obs.get("units"),
                    "precision": obs.get("precision", 2),
                    "observationDate": obs.get("observationDate"),
                }
            )
        if summaries:
            events[event_index]["latestData"] = summaries
    return events


def latest_release_date_for_key(
    events: list[dict[str, Any]], key: str, today: date
) -> tuple[str | None, str | None]:
    matches = [
        event for event in events
        if key in event.get("dataKeys", []) and str(event.get("date", "")) <= today.isoformat()
    ]
    if not matches:
        return None, None
    event = max(matches, key=lambda item: str(item.get("date", "")))
    return str(event.get("date")), str(event.get("name"))


def stable_version_token(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def build_surprise_engine(
    events: list[dict[str, Any]],
    official_data: dict[str, Any],
    previous_payload: dict[str, Any],
    today: date,
) -> dict[str, Any]:
    observations = official_data.get("observations", {})
    previous_engine = previous_payload.get("surpriseEngine", {}) if isinstance(previous_payload, dict) else {}
    previous_shocks = previous_engine.get("shocks", []) if isinstance(previous_engine, dict) else []
    previous_by_id = {
        str(item.get("id")): item for item in previous_shocks
        if isinstance(item, dict) and item.get("id")
    }
    new_shocks: dict[str, dict[str, Any]] = {}
    performance_series: dict[str, dict[str, Any]] = {}

    for key, surprise_spec in SURPRISE_SERIES_SPECS.items():
        observation = observations.get(key)
        if not isinstance(observation, dict):
            continue
        forecast = observation.get("forecast")
        if not isinstance(forecast, dict) or forecast.get("surpriseZ") is None:
            continue
        shock_id = f"release-{key}-{observation.get('observationDate')}"
        previous = previous_by_id.get(shock_id)
        if isinstance(previous, dict) and previous.get("releaseDate"):
            # The observation period has not changed. Preserve its original
            # release date rather than mis-associating it with a newer calendar
            # date that FRED has scheduled but not populated yet.
            release_date = str(previous.get("releaseDate"))
            release_name = str(previous.get("releaseName") or previous.get("releaseFamily") or surprise_spec.get("family"))
        else:
            release_date, release_name = latest_release_date_for_key(events, key, today)
        if not release_date:
            # If FRED has updated the series before the calendar endpoint reflects
            # the release, fall back to retrieval date while clearly flagging it.
            release_date = str(observation.get("retrievedAt", today.isoformat()))[:10]
            release_name = str(surprise_spec.get("family", observation.get("label", key)))
        surprise_z = float(forecast.get("surpriseZ", 0.0))
        channels = [
            {
                "target": target,
                "coefficientPerSigma": coefficient,
                "effect": round(coefficient * surprise_z, 5),
            }
            for target, coefficient in surprise_spec.get("channels", {}).items()
            if abs(coefficient * surprise_z) > 1e-9
        ]
        version_basis = {
            "actual": observation.get("actual"),
            "expected": forecast.get("expected"),
            "revision": observation.get("revision"),
            "releaseDate": release_date,
        }
        version_id = f"{shock_id}-{stable_version_token(version_basis)}"
        revision_detected = bool(
            previous and (
                previous.get("versionId") != version_id
                or previous.get("actual") != observation.get("actual")
            )
        )
        previous_effects = {
            str(channel.get("target")): float(channel.get("effect", 0.0))
            for channel in previous.get("channels", [])
        } if isinstance(previous, dict) else {}
        revision_effects = [
            {
                "target": channel["target"],
                "effect": round(float(channel["effect"]) - previous_effects.get(channel["target"], 0.0), 5),
            }
            for channel in channels
            if abs(float(channel["effect"]) - previous_effects.get(channel["target"], 0.0)) > 1e-9
        ]
        shock = {
            "id": shock_id,
            "versionId": version_id,
            "seriesKey": key,
            "seriesId": observation.get("seriesId"),
            "label": observation.get("label"),
            "releaseFamily": surprise_spec.get("family"),
            "releaseName": release_name,
            "releaseDate": release_date,
            "observationDate": observation.get("observationDate"),
            "actual": observation.get("actual"),
            "expected": forecast.get("expected"),
            "nextExpected": forecast.get("nextExpected"),
            "previous": observation.get("previous"),
            "revision": observation.get("revision"),
            "surprise": forecast.get("surprise"),
            "errorScale": forecast.get("errorScale"),
            "surpriseZ": forecast.get("surpriseZ"),
            "forecastMethod": forecast.get("method"),
            "confidence": surprise_spec.get("confidence", 0.75),
            "durationMonths": surprise_spec.get("durationMonths", 4),
            "peakMonth": surprise_spec.get("peakMonth", 0),
            "decayRate": surprise_spec.get("decayRate", 0.4),
            "channels": channels,
            "revisionDetected": revision_detected,
            "revisionEffects": revision_effects,
            "source": "FRED official release",
        }
        new_shocks[shock_id] = shock
        performance_series[key] = {
            "label": observation.get("label"),
            "seriesId": observation.get("seriesId"),
            **(forecast.get("performance") or {}),
        }

    # Preserve still-live older shocks so their information decays rather than
    # vanishing when a new monthly observation becomes the latest series value.
    for old in previous_shocks:
        if not isinstance(old, dict) or not old.get("id") or old.get("id") in new_shocks:
            continue
        try:
            released = date.fromisoformat(str(old.get("releaseDate")))
            keep_until = released + timedelta(days=int(old.get("durationMonths", 4)) * 31 + 45)
        except (TypeError, ValueError):
            continue
        if today <= keep_until:
            new_shocks[str(old["id"])] = old

    shocks = sorted(
        new_shocks.values(),
        key=lambda item: (str(item.get("releaseDate", "")), str(item.get("seriesKey", ""))),
        reverse=True,
    )
    perf_rows = [row for row in performance_series.values() if row.get("backtestCount")]
    aggregate = {
        "seriesCount": len(perf_rows),
        "meanDirectionalAccuracy": round(
            sum(float(row.get("directionalAccuracy") or 0.0) for row in perf_rows) / len(perf_rows), 3
        ) if perf_rows else None,
        "totalBacktests": sum(int(row.get("backtestCount") or 0) for row in perf_rows),
    }
    return {
        "schemaVersion": 1,
        "status": "active" if shocks else "unavailable",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modelVersion": "MACROSCOPE Phase C v4.4",
        "forecastMethod": "Weighted three-period trend with rolling historical error normalization",
        "shocks": shocks,
        "activeShockCount": len(shocks),
        "performance": {"aggregate": aggregate, "series": performance_series},
        "disclosure": "Forecasts and transmission coefficients are transparent model estimates, not market consensus or causal estimates.",
    }


def attach_surprises_to_events(
    events: list[dict[str, Any]],
    official_data: dict[str, Any],
    surprise_engine: dict[str, Any],
    today: date,
) -> list[dict[str, Any]]:
    observations = official_data.get("observations", {})
    shocks = surprise_engine.get("shocks", [])
    shock_by_series_date = {
        (str(shock.get("seriesKey")), str(shock.get("releaseDate"))): shock
        for shock in shocks if isinstance(shock, dict)
    }
    earliest_upcoming: dict[str, int] = {}
    for index, event in enumerate(events):
        if str(event.get("date", "")) < today.isoformat():
            continue
        for key in event.get("dataKeys", []):
            prior = earliest_upcoming.get(str(key))
            if prior is None or str(event.get("date", "")) < str(events[prior].get("date", "")):
                earliest_upcoming[str(key)] = index

    for index, event in enumerate(events):
        keys = event.get("dataKeys", [])
        if not isinstance(keys, list):
            continue
        release_surprises = []
        model_forecasts = []
        for key in keys:
            obs = observations.get(key, {})
            if event.get("date", "") <= today.isoformat():
                shock = shock_by_series_date.get((str(key), str(event.get("date"))))
                if shock:
                    release_surprises.append({
                        "seriesKey": key,
                        "label": shock.get("label"),
                        "actual": shock.get("actual"),
                        "expected": shock.get("expected"),
                        "surprise": shock.get("surprise"),
                        "surpriseZ": shock.get("surpriseZ"),
                        "units": obs.get("units"),
                        "precision": obs.get("precision", 2),
                        "revisionDetected": shock.get("revisionDetected", False),
                    })
            elif earliest_upcoming.get(str(key)) == index:
                forecast = obs.get("forecast") if isinstance(obs, dict) else None
                if isinstance(forecast, dict) and forecast.get("nextExpected") is not None:
                    model_forecasts.append({
                        "seriesKey": key,
                        "label": obs.get("label"),
                        "expected": forecast.get("nextExpected"),
                        "errorScale": forecast.get("errorScale"),
                        "units": obs.get("units"),
                        "precision": obs.get("precision", 2),
                        "method": forecast.get("method"),
                    })
        if release_surprises:
            event["releaseSurprises"] = release_surprises
        if model_forecasts:
            event["modelForecasts"] = model_forecasts
    return events


def usable_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("events")
    return events if isinstance(events, list) and events else []


def write_output(
    config: Config,
    start: date,
    end: date,
    events: list[dict[str, Any]],
    official_data: dict[str, Any],
    surprise_engine: dict[str, Any],
    calendar_fallback: bool,
) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "schemaVersion": 3,
        "generatedAt": now.isoformat(timespec="seconds"),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "source": {
            "name": "FRED Economic Release Calendar and Series Observations",
            "provider": "Federal Reserve Bank of St. Louis",
            "endpoints": ["fred/releases/dates", "fred/series/observations"],
        },
        "calendarFallback": calendar_fallback,
        "eventCount": len(events),
        "events": events,
        "officialData": official_data,
        "surpriseEngine": surprise_engine,
    }
    config.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.output.with_suffix(config.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(config.output)


def main() -> int:
    config: Config | None = None
    try:
        config = load_config()
        today = date.today()
        start = today - timedelta(days=config.history_days)
        end = today + timedelta(days=config.future_days)
        previous_payload = read_existing_payload(config.output)

        calendar_fallback = False
        try:
            raw = collect_release_dates(config, start, end)
            events = normalize_events(raw, today)
            if not events:
                raise RuntimeError("FRED returned no matching major releases")
        except Exception as exc:
            old_events = usable_events(previous_payload)
            if config.allow_stale and old_events:
                calendar_fallback = True
                events = old_events
                print(
                    f"::warning::Calendar refresh failed; preserving prior release calendar: {exc}",
                    file=sys.stderr,
                )
            else:
                raise

        official_data = collect_official_data(config, today, previous_payload)
        events = attach_data_to_events(events, official_data, today)
        surprise_engine = build_surprise_engine(events, official_data, previous_payload, today)
        events = attach_surprises_to_events(events, official_data, surprise_engine, today)
        write_output(config, start, end, events, official_data, surprise_engine, calendar_fallback)

        print(f"Wrote {len(events)} calendar events to {config.output}")
        print(
            f"Official data: {official_data['seriesCount']} series "
            f"({official_data['status']})"
        )
        print(
            f"Phase C surprises: {surprise_engine['activeShockCount']} active shocks; "
            f"{surprise_engine['performance']['aggregate']['totalBacktests']} backtests"
        )
        return 0
    except Exception as exc:
        print(f"update_calendar.py: {exc}", file=sys.stderr)
        if config is not None and config.allow_stale:
            previous = read_existing_payload(config.output)
            if usable_events(previous):
                print(
                    f"::warning::Update failed; preserving existing snapshot at {config.output} "
                    "and allowing Pages deployment to continue.",
                    file=sys.stderr,
                )
                return 0
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
