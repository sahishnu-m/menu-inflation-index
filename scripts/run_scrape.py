"""
Run one collection pass over the configured restaurants.

Run from the repository root:
    python scripts/run_scrape.py --dry-run          # look, store nothing
    python scripts/run_scrape.py --only chipotle    # one restaurant
    python scripts/run_scrape.py                    # the real monthly run
    python scripts/run_scrape.py --no-cache         # ignore cached pages

This is a SEPARATE SCRIPT from the dashboard on purpose. Scraping reaches
other people's servers, so it should happen because you decided to run it.
Wired into app.py, every visitor to the deployed dashboard would send requests
to twenty restaurants without knowing it.

Expect it to be slow. The rate limit is three seconds per host, and that is
the scraper behaving correctly.

Read the header of menu_index/scraper.py for the full set of rules it follows.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from menu_index.config import load_restaurants  # noqa: E402
from menu_index.pipeline import current_period, run  # noqa: E402
from menu_index.scraper import cache_summary  # noqa: E402

STATUS_MARK = {
    "fetched": "[ok]   ",
    "skipped": "[skip] ",
    "error": "[err]  ",
    "dry-run": "[dry]  ",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="restaurant ids to run")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and print without writing to the database")
    parser.add_argument("--no-cache", action="store_true",
                        help="re-fetch even when a cached page is recent")
    parser.add_argument("--quiet", action="store_true", help="less logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    restaurants = load_restaurants()
    print(f"Collecting menu prices for {current_period()}")
    print(f"{len(restaurants)} restaurants configured, "
          f"{sum(1 for r in restaurants if r.verified)} of them verified")
    print("robots.txt is checked first for every site and is binding.")
    print("One request every 3 seconds per host.\n")

    report = run(only=args.only, dry_run=args.dry_run, use_cache=not args.no_cache)

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    for row in report.per_restaurant:
        mark = STATUS_MARK.get(row["status"], "[?]    ")
        print(f"{mark} {row['restaurant']:<24} {row['items']:>4} items  {row['reason']}")

    print("\n" + report.summary_line())

    if report.matched_renames:
        print(f"\nMatched {len(report.matched_renames)} renamed item(s) to existing series:")
        for old, new, score in report.matched_renames[:10]:
            print(f"    {old!r} <- {new!r}  (similarity {score})")

    cache = cache_summary()
    print(f"\nPage cache: {cache['files']} files, {cache['megabytes']} MB "
          "(excluded from git)")

    if report.fetched == 0:
        print(
            "\nNothing was collected. That is the expected result until the\n"
            "selectors in config/restaurants.yaml have been checked against\n"
            "the live pages. See the instructions at the top of that file, and\n"
            "use --dry-run while you work on one."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
