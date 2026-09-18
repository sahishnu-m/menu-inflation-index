"""
Generate simulated historical data so the dashboard has something to show.

Run from the repository root:
    python scripts/seed_demo_data.py
    python scripts/seed_demo_data.py --months 18

EVERYTHING THIS WRITES IS LABELLED SIMULATED. It exists to exercise the
pipeline and give the charts a shape before real collection has run for long
enough to plot. It describes nothing about actual Reno prices.

Remove it at any time with:
    python scripts/seed_demo_data.py --clear
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from menu_index.database import SIMULATED, clear_dataset  # noqa: E402
from menu_index.seed import generate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=14,
                        help="how many months of history to generate (default 14)")
    parser.add_argument("--seed", type=int, default=20260917,
                        help="random seed; the same seed gives the same data")
    parser.add_argument("--clear", action="store_true",
                        help="delete the simulated data and write nothing new")
    args = parser.parse_args()

    if args.clear:
        removed = clear_dataset(SIMULATED)
        print(f"Removed {removed:,} simulated observations.")
        print("Real observations were untouched.")
        return 0

    print("Generating SIMULATED demo data.")
    print("None of this describes real Reno prices. Every row is labelled")
    print("dataset='simulated' and stays separate from real observations.\n")

    stats = generate(months=args.months, seed=args.seed)

    if stats["removed"]:
        print(f"Replaced {stats['removed']:,} earlier simulated observations.")
    print(f"Restaurants:  {stats['restaurants']}")
    print(f"Months:       {stats['periods']}  ({stats['first_period']} to {stats['last_period']})")
    print(f"Items:        {stats['items']:,}")
    print(f"Observations: {stats['observations']:,}")
    print("\nStart the dashboard with:  streamlit run app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
