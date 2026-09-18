"""
pipeline.py - runs one full collection: fetch, parse, match, store.

Keeping this orchestration in its own file means scraper.py, parser.py and
normalize.py each stay a single idea, while the order they run in lives in one
readable place.

WHAT ONE RUN DOES, PER RESTAURANT
    1. Fetch the menu page, obeying robots.txt, the rate limit and the cache.
    2. Parse it into names and prices using the configured selectors.
    3. Match each name against items already known for that restaurant, so a
       renamed item continues its existing series.
    4. Write one observation per item for the current month.
    5. Record what happened in scrape_log, including the failures.

Failures are recorded rather than raised. One restaurant blocking the scraper
should leave the other nineteen collected, and the log is what turns a silent
gap in the data into something you can read and act on.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from .config import Restaurant, load_restaurants
from .database import (
    REAL,
    connect,
    existing_items,
    log_scrape,
    record_observation,
    sync_restaurants,
    upsert_item,
)
from .normalize import extract_size, find_match, make_match_key
from .parser import parse_menu
from .scraper import PoliteFetcher

logger = logging.getLogger(__name__)


@dataclass
class RunReport:
    """What one full run achieved."""

    period: str
    fetched: int = 0
    skipped: int = 0
    errored: int = 0
    items_written: int = 0
    new_items: int = 0
    matched_renames: list[tuple[str, str, float]] = field(default_factory=list)
    per_restaurant: list[dict] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"{self.period}: {self.fetched} fetched, {self.skipped} skipped, "
            f"{self.errored} errored, {self.items_written} prices stored "
            f"({self.new_items} new items)"
        )


def current_period() -> str:
    """This calendar month as 'YYYY-MM'."""
    today = dt.date.today()
    return f"{today.year:04d}-{today.month:02d}"


def run(
    only: list[str] | None = None,
    dry_run: bool = False,
    use_cache: bool = True,
    period: str | None = None,
) -> RunReport:
    """
    Collect prices for every configured restaurant.

    Args:
        only:      restaurant ids to limit the run to, for testing one site
        dry_run:   parse and report while writing nothing to the database
        use_cache: serve pages from the local cache when they are recent
        period:    override the month, mainly useful in tests
    """
    period = period or current_period()
    report = RunReport(period=period)
    fetcher = PoliteFetcher()

    restaurants = load_restaurants()
    if only:
        wanted = set(only)
        restaurants = tuple(r for r in restaurants if r.id in wanted)
        if not restaurants:
            raise ValueError(f"No restaurant matched {sorted(wanted)}")

    for restaurant in restaurants:
        outcome = _run_one(fetcher, restaurant, period, dry_run, use_cache, report)
        report.per_restaurant.append(outcome)

    return report


def _run_one(
    fetcher: PoliteFetcher,
    restaurant: Restaurant,
    period: str,
    dry_run: bool,
    use_cache: bool,
    report: RunReport,
) -> dict:
    """Handle a single restaurant, returning a row for the report."""
    result = fetcher.fetch(restaurant, use_cache=use_cache)

    if result.status == "skipped":
        report.skipped += 1
        if not dry_run:
            with connect() as connection:
                sync_restaurants(connection)
                log_scrape(connection, restaurant.id, "skipped", result.reason)
        return {"restaurant": restaurant.id, "status": "skipped",
                "reason": result.reason, "items": 0}

    if result.status == "error" or not result.html:
        report.errored += 1
        if not dry_run:
            with connect() as connection:
                sync_restaurants(connection)
                log_scrape(connection, restaurant.id, "error", result.reason)
        return {"restaurant": restaurant.id, "status": "error",
                "reason": result.reason, "items": 0}

    parsed = parse_menu(restaurant, result.html)
    report.fetched += 1

    if dry_run:
        for item in parsed.items[:20]:
            size = f" [{item.size_value:g} {item.size_unit}]" if item.size_value else ""
            print(f"    {item.observed_name}{size}  ${item.price_cents / 100:.2f}")
        return {"restaurant": restaurant.id, "status": "dry-run",
                "reason": parsed.notes, "items": parsed.count}

    written, created = _store(restaurant, parsed.items, period, report)
    report.items_written += written
    report.new_items += created

    with connect() as connection:
        log_scrape(connection, restaurant.id, "fetched", parsed.notes, parsed.count)

    return {"restaurant": restaurant.id, "status": "fetched",
            "reason": parsed.notes, "items": written}


def _store(restaurant: Restaurant, items, period: str, report: RunReport) -> tuple[int, int]:
    """
    Write parsed items, linking each to an existing series where one fits.

    This is where a rename gets absorbed. The cleaned key for today's name is
    compared against the keys already stored for this restaurant, and a close
    enough match reuses that item id so the price series continues. A match
    below the threshold creates a new item instead.
    """
    written = 0
    created = 0

    with connect() as connection:
        sync_restaurants(connection)

        known = existing_items(connection, restaurant.id)
        # Keys are compared within a (size_value, size_unit) group, so a 16oz
        # drink never merges into a 12oz one. A size change is a genuine
        # product change and deserves its own series.
        keys_by_size: dict[tuple, dict[str, int]] = {}
        for row in known:
            bucket = keys_by_size.setdefault((row["size_value"], row["size_unit"]), {})
            bucket[row["match_key"]] = row["id"]

        for item in items:
            name_without_size, size_value, size_unit = extract_size(item.observed_name)
            # Prefer the size parsed from the name during parsing, falling back
            # to whatever this pass found.
            size_value = item.size_value if item.size_value is not None else size_value
            size_unit = item.size_unit or size_unit

            key = make_match_key(name_without_size)
            if not key:
                continue

            bucket = keys_by_size.setdefault((size_value, size_unit), {})
            matched_key, score = find_match(key, list(bucket.keys()))

            if matched_key and matched_key != key:
                report.matched_renames.append((matched_key, key, round(score, 3)))

            canonical = item.observed_name
            store_key = matched_key or key
            if matched_key is None:
                created += 1

            item_id = upsert_item(
                connection,
                restaurant_id=restaurant.id,
                canonical_name=canonical,
                match_key=store_key,
                size_value=size_value,
                size_unit=size_unit,
                seen_on=period,
            )
            bucket[store_key] = item_id

            record_observation(
                connection,
                item_id=item_id,
                period=period,
                price_cents=item.price_cents,
                observed_name=item.observed_name,
                dataset=REAL,
            )
            written += 1

    return written, created
