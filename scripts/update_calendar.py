#!/usr/bin/env python3
"""Generate a static U.S. economic-release calendar for MACROSCOPE.

The script calls the official FRED releases/dates endpoint using the
FRED_API_KEY environment variable, keeps a rolling year of history plus a
configurable future window, filters the feed to major U.S. macro releases,
and writes data/economic-calendar.json for GitHub Pages.

Usage:
    FRED_API_KEY=your_key python scripts/update_calendar.py

Optional environment variables:
    CALENDAR_HISTORY_DAYS=365
    CALENDAR_FUTURE_DAYS=180
    CALENDAR_OUTPUT=data/economic-calendar.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"

# FRED contains many daily market and international releases. These patterns
# retain the releases most useful to a U.S. macroeconomic outlook terminal.
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

# Some recurring releases are useful but may not match the exact patterns
# above. Add or remove exact FRED release IDs here after inspecting the output.
INCLUDE_RELEASE_IDS: set[int] = set()
EXCLUDE_RELEASE_IDS: set[int] = set()


@dataclass(frozen=True)
class Config:
    api_key: str
    history_days: int
    future_days: int
    output: Path


def load_config() -> Config:
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "FRED_API_KEY is not set. Add it as a GitHub Actions secret or "
            "export it before running this script."
        )

    return Config(
        api_key=api_key,
        history_days=max(1, int(os.environ.get("CALENDAR_HISTORY_DAYS", "365"))),
        future_days=max(1, int(os.environ.get("CALENDAR_FUTURE_DAYS", "180"))),
        output=Path(os.environ.get("CALENDAR_OUTPUT", "data/economic-calendar.json")),
    )


def fred_get(params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    query = urlencode(params)
    request = Request(
        f"{FRED_RELEASE_DATES_URL}?{query}",
        headers={"User-Agent": "MACROSCOPE-economic-calendar/1.0"},
    )

    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if 500 <= exc.code < 600 and attempt < retries:
                time.sleep(attempt * 2)
                continue
            raise RuntimeError(f"FRED HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(attempt * 2)
                continue
            raise RuntimeError(f"Could not reach FRED: {exc}") from exc

    raise RuntimeError("FRED request failed after retries")


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


def collect_release_dates(config: Config, start: date, end: date) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    offset = 0
    limit = 1000

    while True:
        payload = fred_get(
            {
                "api_key": config.api_key,
                "file_type": "json",
                "realtime_start": start.isoformat(),
                "realtime_end": end.isoformat(),
                "include_release_dates_with_no_data": "true",
                "limit": limit,
                "offset": offset,
                "sort_order": "asc",
            }
        )

        batch = payload.get("release_dates", [])
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected FRED response: release_dates is not a list")

        events.extend(item for item in batch if isinstance(item, dict) and is_major_release(item))

        count = int(payload.get("count", len(batch)))
        returned_offset = int(payload.get("offset", offset))
        returned_limit = int(payload.get("limit", limit))
        next_offset = returned_offset + returned_limit
        if next_offset >= count or not batch:
            break
        offset = next_offset

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
        event = {
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
        unique[(raw_date, release_id)] = event

    return sorted(unique.values(), key=lambda event: (event["date"], event["name"]))


def write_output(config: Config, start: date, end: date, events: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "schemaVersion": 1,
        "generatedAt": now.isoformat(timespec="seconds"),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "source": {
            "name": "FRED Economic Release Calendar",
            "provider": "Federal Reserve Bank of St. Louis",
            "endpoint": "fred/releases/dates",
        },
        "eventCount": len(events),
        "events": events,
    }

    config.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.output.with_suffix(config.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(config.output)


def main() -> int:
    try:
        config = load_config()
        today = date.today()
        start = today - timedelta(days=config.history_days)
        end = today + timedelta(days=config.future_days)
        raw = collect_release_dates(config, start, end)
        events = normalize_events(raw, today)
        write_output(config, start, end, events)
        print(f"Wrote {len(events)} events to {config.output}")
        return 0
    except Exception as exc:  # GitHub Actions should show one clear error.
        print(f"update_calendar.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
