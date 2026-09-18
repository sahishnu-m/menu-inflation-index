"""
Fetch the BLS CPI series and cache it locally.

Run from the repository root:
    python scripts/fetch_cpi.py
    python scripts/fetch_cpi.py --years 10

The dashboard fetches this itself when the cache is stale, so running this by
hand is mostly useful for the scheduled workflow, where refreshing the cache in
its own step keeps a BLS outage from affecting the collected prices.

The endpoint answers without a registration key. Setting BLS_API_KEY in the
environment raises the daily request cap.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from menu_index.bls import BLSUnavailable, fetch_cpi  # noqa: E402
from menu_index.config import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=6,
                        help="how many years back to request (default 6)")
    args = parser.parse_args()

    settings = load_settings()["bls"]
    end_year = dt.date.today().year
    start_year = end_year - args.years + 1

    print(f"Series:  {settings['series_id']}  ({settings['series_name']})")
    print(f"Range:   {start_year} to {end_year}")
    print(f"API key: {'set' if os.environ.get('BLS_API_KEY') else 'none (using the public cap)'}\n")

    try:
        frame = fetch_cpi(start_year=start_year, end_year=end_year, use_cache=False)
    except BLSUnavailable as exc:
        print(f"Could not fetch the series: {exc}")
        return 1

    print(f"Retrieved {len(frame)} monthly observations.")
    print(f"Earliest:  {frame.iloc[0]['period']}  {frame.iloc[0]['cpi']}")
    print(f"Latest:    {frame.iloc[-1]['period']}  {frame.iloc[-1]['cpi']}")

    first, last = float(frame.iloc[0]["cpi"]), float(frame.iloc[-1]["cpi"])
    print(f"\nChange across the window: {(last / first - 1) * 100:+.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
