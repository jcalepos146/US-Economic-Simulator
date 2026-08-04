#!/usr/bin/env python3
"""Generate a static U.S. economic-release calendar for MACROSCOPE.

This script is designed for GitHub Actions + GitHub Pages. It:
- reads FRED_API_KEY from the Actions environment;
- downloads a rolling historical/future release calendar from FRED;
- splits the requested date range into smaller windows to reduce timeouts;
- retries transient HTTP/network failures with exponential backoff;
- writes data/economic-calendar.json atomically;
- preserves a previously generated non-empty calendar if FRED is temporarily
  unreachable, allowing Pages deployment to continue with stale data.

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

import json
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

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"

# FRED contains many daily market and international releases. These patterns
# retain releases useful to a U.S. macroeconomic outlook terminal.
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
    # 2, 4, 8, 16, 32... seconds, plus a small randomized spread.
    return min(45.0, 2.0 ** attempt) + random.uniform(0.2, 1.2)


def fred_get(params: dict[str, Any], config: Config) -> dict[str, Any]:
    query = urlencode(params)
    url = f"{FRED_RELEASE_DATES_URL}?{query}"

    for attempt in range(1, config.max_retries + 1):
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "MACROSCOPE-economic-calendar/2.0",
            },
        )

        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise RuntimeError("Unexpected FRED response: root value is not an object")
            return payload

        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            transient = exc.code == 429 or 500 <= exc.code < 600
            if transient and attempt < config.max_retries:
                delay = retry_delay(attempt, exc.headers.get("Retry-After"))
                print(
                    f"FRED HTTP {exc.code}; retry {attempt}/{config.max_retries} "
                    f"in {delay:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"FRED HTTP {exc.code}: {detail}") from exc

        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt < config.max_retries:
                delay = retry_delay(attempt)
                print(
                    f"FRED network error ({exc}); retry "
                    f"{attempt}/{config.max_retries} in {delay:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"Could not reach FRED after {config.max_retries} attempts: {exc}"
            ) from exc

        except json.JSONDecodeError as exc:
            if attempt < config.max_retries:
                delay = retry_delay(attempt)
                print(
                    f"FRED returned invalid JSON; retry {attempt}/{config.max_retries} "
                    f"in {delay:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise RuntimeError("FRED returned invalid JSON after repeated attempts") from exc

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

        events.extend(
            item for item in batch if isinstance(item, dict) and is_major_release(item)
        )

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

        status = (
            "released"
            if event_date < today
            else "today"
            if event_date == today
            else "scheduled"
        )
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
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(config.output)


def existing_calendar_is_usable(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    events = payload.get("events")
    return isinstance(events, list) and len(events) > 0 and bool(payload.get("generatedAt"))


def main() -> int:
    config: Config | None = None
    try:
        config = load_config()
        today = date.today()
        start = today - timedelta(days=config.history_days)
        end = today + timedelta(days=config.future_days)

        raw = collect_release_dates(config, start, end)
        events = normalize_events(raw, today)
        if not events:
            raise RuntimeError(
                "FRED returned no matching major releases; refusing to replace the calendar "
                "with an empty file."
            )

        write_output(config, start, end, events)
        print(f"Wrote {len(events)} events to {config.output}")
        return 0

    except Exception as exc:
        print(f"update_calendar.py: {exc}", file=sys.stderr)

        if (
            config is not None
            and config.allow_stale
            and existing_calendar_is_usable(config.output)
        ):
            print(
                f"::warning::FRED update failed; preserving existing calendar at "
                f"{config.output} and allowing Pages deployment to continue.",
                file=sys.stderr,
            )
            return 0

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
