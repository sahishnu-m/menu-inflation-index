"""
seed.py - generates plausible historical data so the dashboard has something
to display before months of real collection have happened.

EVERY ROW THIS FILE WRITES IS MARKED `dataset = 'simulated'`.

That label is the whole design. Simulated numbers exist to prove the pipeline
works and to give the charts a shape while real data accumulates. They
describe nothing about Reno. Mixing them into a real result would produce a
figure that looks like a measurement and is an invention, so:

  * every row written here carries dataset='simulated'
  * every read in database.py requires a dataset argument
  * the index is always built from one dataset at a time
  * the dashboard shows a standing notice whenever it displays simulated data

Deleting the simulated rows and keeping the real ones is a single call to
database.clear_dataset('simulated').

HOW THE NUMBERS ARE BUILT
The aim is data that exercises every code path, rather than data that looks
impressive. It therefore includes the awkward cases on purpose:

  * a gentle upward drift, roughly matching real restaurant inflation
  * per-category drift, since fast food and mid-range move differently
  * prices that stay put for months and then jump, which is how menus behave
  * items that get renamed partway through, exercising the matcher
  * items that disappear, exercising survivorship handling
  * items that shrink in size at the same price, the shrinkflation case
  * psychological pricing, so results land on .99 and .95 like real menus

A fixed random seed makes every run produce the same data, so a chart that
looks odd can be investigated instead of vanishing on the next run.
"""

from __future__ import annotations

import datetime as dt
import random
import re

from .config import Restaurant, load_restaurants
from .database import (
    SIMULATED,
    clear_dataset,
    connect,
    log_scrape,
    record_observation,
    sync_restaurants,
    upsert_item,
)
from .normalize import extract_size, make_match_key

# Menu templates by category: (item name, typical price in dollars).
# Prices sit near real Reno levels so the demo reads plausibly.
#
# The names are deliberately GENERIC rather than each restaurant's real
# dishes. Writing out twenty actual menus would imply this project knows what
# those restaurants sell and charge, which is exactly the knowledge it has yet
# to collect. Generic names keep the demo obviously a demo while still
# exercising every code path.
MENU_TEMPLATES = {
    "fast_food": [
        ("Burger", 3.29), ("Double Burger", 5.49),
        ("Chicken Sandwich", 5.99), ("French Fries (Large)", 3.79),
        ("Soft Drink (32 oz)", 2.29), ("Breakfast Sandwich", 4.49),
        ("Side Salad", 3.49), ("Combo Meal", 9.29),
        ("Kids Meal", 5.29), ("Milkshake (16 oz)", 4.59),
    ],
    "casual": [
        ("Classic Burger", 14.50), ("Fish and Chips", 17.95),
        ("Caesar Salad", 12.75), ("Chicken Wings (12 pc)", 16.50),
        ("Margherita Pizza", 15.95), ("Club Sandwich", 13.95),
        ("Loaded Nachos", 13.50), ("Soup of the Day", 7.25),
        ("Veggie Bowl", 12.95), ("Pancake Breakfast", 11.50),
        ("Mac and Cheese", 11.95), ("Pulled Pork Sandwich", 14.25),
    ],
    "mid_range": [
        ("Ribeye Steak (12 oz)", 42.00), ("Pan Seared Salmon", 31.50),
        ("Roast Chicken", 27.00), ("Mushroom Risotto", 24.50),
        ("Charcuterie Board", 22.00), ("Braised Short Rib", 34.00),
        ("Seasonal Vegetable Plate", 21.00), ("House Pasta", 25.50),
        ("Duck Confit", 33.00), ("Beet Salad", 15.00),
    ],
}

# Monthly price drift by category, as a fraction. Mid-range menus tend to be
# reprinted less often and move in larger, rarer steps; fast food repriced
# more often in smaller ones.
MONTHLY_DRIFT = {
    "fast_food": 0.0045,   # about 5.5% a year
    "casual": 0.0038,      # about 4.7% a year
    "mid_range": 0.0030,   # about 3.7% a year
}

# Chance in any month that a restaurant reprices at all. Menus change on a
# schedule rather than continuously, which is what makes a real series look
# like a staircase instead of a smooth line.
REPRICE_CHANCE = {"fast_food": 0.30, "casual": 0.22, "mid_range": 0.16}

RENAME_TEMPLATES = [
    ("Classic Burger", "The Burger"),
    ("Chicken Sandwich", "Crispy Chicken Sandwich"),
    ("Caesar Salad", "Classic Caesar"),
    ("Soup of the Day", "Daily Soup"),
]


def _rewrite_size(name: str, new_size: float) -> str:
    """
    Rewrite a name with a new portion size, the way a menu reads after a
    shrink: "Soft Drink (32 oz)" with new_size 24 becomes "Soft Drink (24 oz)".
    """
    def replace(match: re.Match) -> str:
        return match.group(0).replace(match.group(1), f"{new_size:g}", 1)

    return re.sub(
        r"(\d+(?:\.\d+)?)(\s*(?:oz|ounces?|pcs?|pieces?))",
        replace, name, count=1,
    )


def _psychological_price(dollars: float) -> int:
    """
    Round a price the way a menu would, and return cents.

    Real menus land on .99, .95, .50 and whole dollars rather than arbitrary
    figures. Cheap items usually end .99 or .49; expensive ones round to whole
    or half dollars. Modelling that keeps the demo data from looking obviously
    generated, and it exercises the parser against realistic strings.
    """
    if dollars < 10:
        base = int(dollars)
        ending = random.choice([0.29, 0.49, 0.79, 0.99])
        return int(round((base + ending) * 100))
    if dollars < 25:
        base = int(dollars)
        ending = random.choice([0.50, 0.95, 0.99, 0.00])
        return int(round((base + ending) * 100))
    return int(round(round(dollars * 2) / 2 * 100))  # nearest 50 cents


def _months_back(count: int) -> list[str]:
    """The last `count` calendar months as 'YYYY-MM', oldest first."""
    today = dt.date.today().replace(day=1)
    periods = []
    for step in range(count - 1, -1, -1):
        year = today.year
        month = today.month - step
        while month <= 0:
            month += 12
            year -= 1
        periods.append(f"{year:04d}-{month:02d}")
    return periods


def generate(months: int = 14, seed: int = 20260917, replace: bool = True) -> dict:
    """
    Write `months` of simulated observations.

    Returns counts for the caller to print.

    `replace` clears any existing simulated rows first, so regenerating
    produces one clean dataset instead of stacking a second copy on the first.
    It touches only the simulated dataset and can never reach real rows.
    """
    random.seed(seed)  # deterministic: the same run produces the same data

    removed = clear_dataset(SIMULATED) if replace else 0
    periods = _months_back(months)
    restaurants = load_restaurants()

    written = 0
    items_created = 0

    with connect() as connection:
        sync_restaurants(connection)

        for restaurant in restaurants:
            template = MENU_TEMPLATES.get(restaurant.category, [])
            if not template:
                continue

            # Each restaurant carries its own menu of 6 to 10 items, and its
            # own price level, so two restaurants in a category differ the way
            # real ones do.
            menu_size = random.randint(6, min(10, len(template)))
            menu = random.sample(template, menu_size)
            house_factor = random.uniform(0.88, 1.15)

            # Items this restaurant will rename or drop partway through.
            rename_at = random.choice(periods[3:-2]) if len(periods) > 6 else None
            renames = dict(RENAME_TEMPLATES)
            drop_after = random.choice(periods[2:-1]) if random.random() < 0.35 else None
            dropped_item = random.choice(menu)[0] if drop_after else None

            # An item that shrinks in size while holding its price.
            shrink_candidates = [n for n, _ in menu if extract_size(n)[1] is not None]
            shrink_item = random.choice(shrink_candidates) if shrink_candidates and random.random() < 0.4 else None
            shrink_at = random.choice(periods[4:-1]) if shrink_item and len(periods) > 6 else None

            current = {name: base * house_factor for name, base in menu}

            for period in periods:
                # Does the restaurant reprint its menu this month?
                reprices = random.random() < REPRICE_CHANCE.get(restaurant.category, 0.2)
                drift = MONTHLY_DRIFT.get(restaurant.category, 0.004)

                for name, _ in menu:
                    if drop_after and name == dropped_item and period > drop_after:
                        continue  # item left the menu

                    if reprices:
                        # A reprice moves the whole menu by roughly the drift,
                        # with noise, and occasionally cuts a price. Real menus
                        # do lower prices sometimes, and a model that only ever
                        # rises would hide that from the index.
                        months_of_drift = random.uniform(2.0, 5.0)
                        step = drift * months_of_drift
                        if random.random() < 0.10:
                            step = -abs(step) * random.uniform(0.3, 0.8)
                        current[name] *= 1.0 + step + random.gauss(0, 0.004)

                    display_name = name
                    if rename_at and period >= rename_at and name in renames:
                        display_name = renames[name]

                    size_value, size_unit = extract_size(name)[1:]
                    if shrink_item and name == shrink_item and shrink_at and period >= shrink_at:
                        # Shrinkflation: smaller portion, unchanged price. The
                        # size field is what makes this visible downstream.
                        if size_value:
                            size_value = round(size_value * 0.75, 1)
                            display_name = _rewrite_size(name, size_value)

                    price_cents = _psychological_price(current[name])

                    item_id = upsert_item(
                        connection,
                        restaurant_id=restaurant.id,
                        canonical_name=name,
                        match_key=make_match_key(extract_size(name)[0]),
                        size_value=size_value,
                        size_unit=size_unit,
                        seen_on=period,
                    )
                    record_observation(
                        connection,
                        item_id=item_id,
                        period=period,
                        price_cents=price_cents,
                        observed_name=display_name,
                        dataset=SIMULATED,
                        scraped_at=f"{period}-15T12:00:00",
                    )
                    written += 1

            log_scrape(
                connection, restaurant.id, "fetched",
                f"simulated {len(menu)} items across {len(periods)} months",
                items_found=len(menu),
            )
            items_created += len(menu)

    return {
        "removed": removed,
        "observations": written,
        "items": items_created,
        "periods": len(periods),
        "restaurants": len(restaurants),
        "first_period": periods[0],
        "last_period": periods[-1],
    }
