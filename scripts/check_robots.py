"""
Check what every configured restaurant's robots.txt allows, without scraping.

Run from the repository root:
    python scripts/check_robots.py

This sends one request per site for robots.txt alone and reads no menu pages.
Run it before adding a restaurant, so a site that declines automated access is
known up front rather than discovered halfway through a collection run.

The result is written to data/robots_report.json and shown in the dashboard.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from menu_index.config import DATA_DIR, load_restaurants, user_agent  # noqa: E402
from menu_index.scraper import PoliteFetcher  # noqa: E402

REPORT_PATH = DATA_DIR / "robots_report.json"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

    restaurants = load_restaurants()
    fetcher = PoliteFetcher()

    print(f"Checking robots.txt for {len(restaurants)} sites.")
    print(f"User-Agent: {user_agent()}")
    print("One request every 3 seconds per host. Menu pages are left alone.\n")

    results = []
    for restaurant in restaurants:
        allowed, reason = fetcher.allowed(restaurant.url)
        mark = "[allowed]" if allowed else "[blocked]"
        print(f"{mark} {restaurant.name}")
        print(f"          {restaurant.url}")
        print(f"          {reason}\n")
        results.append({
            "id": restaurant.id,
            "name": restaurant.name,
            "category": restaurant.category,
            "url": restaurant.url,
            "allowed": allowed,
            "reason": reason,
        })

    allowed_count = sum(1 for r in results if r["allowed"])
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "user_agent": user_agent(),
                "allowed": allowed_count,
                "total": len(results),
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=" * 70)
    print(f"{allowed_count} of {len(results)} sites allow this scraper.")
    print(f"Report written to {REPORT_PATH}")
    print(
        "\nA blocked site stays blocked. Changing the User-Agent to look like\n"
        "Chrome would defeat a control the site chose to put in place, so the\n"
        "project records the refusal and works with the sites that permit it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
