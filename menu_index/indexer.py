"""
indexer.py - turns a pile of observed prices into an index number.

WHAT AN INDEX IS, AND WHY THE AVERAGE PRICE WILL NOT DO
The tempting approach is to average every price seen each month and chart it.
That produces a number that moves for the wrong reasons.

Suppose January holds 40 fast-food items and 10 steakhouse entrees, and by
February a steakhouse has published its full menu, so the mix is 40 and 30.
The average price leaps. Nothing got more expensive; the BASKET changed.
Menus also gain expensive specials in December and lose them in January, and
a restaurant that stops publishing prices drops out of the average entirely.

A price index removes that by holding the basket still and letting only prices
move.

THE LASPEYRES INDEX
A Laspeyres index prices a FIXED basket, chosen in a base period, again in
every later period:

    Index(t) = 100 x  sum( price(i, t) x quantity(i, 0) )
                     -----------------------------------
                      sum( price(i, 0) x quantity(i, 0) )

Quantities come from the base period and stay there, so every change in the
number comes from prices. This is the family the CPI belongs to, which is what
makes the comparison in this project reasonable.

The equal-weight simplification, stated plainly: this project has no
quantities. Nobody knows how many cheeseburgers Reno buys. Every basket item
is therefore given a quantity of 1, which reduces the formula to

    Index(t) = 100 x  sum of basket prices this period
                     ----------------------------------
                      sum of the same items' base prices

That treats a $60 steak as mattering as much as a $3 taco, which is a real
weakness and one the README states. The BLS solves it with consumer
expenditure surveys that this project has no access to.

THE MATCHED-ITEM RULE
An item enters the basket only when it appears in BOTH the base period and the
period being priced. Pricing an item that appeared halfway through would mean
the numerator holds something the denominator lacks, and the ratio would
measure menu growth rather than inflation.

CHAINING
Holding one base period forever shrinks the basket as items disappear, until
the index rests on a handful of survivors. Instead the index is CHAINED:
a link is computed between each neighbouring pair of months using the items
present in both, then the links are multiplied together into a series. Each
month is compared against the one before it, where the overlap is largest,
and the chain carries that forward. This is close to how the real CPI handles
items entering and leaving the market.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import load_settings


@dataclass
class IndexPoint:
    """One period of the index."""

    period: str
    value: float
    matched_items: int  # items priced in both this period and the previous one
    link: float  # this period's price ratio against the previous one
    note: str = ""


@dataclass
class IndexResult:
    """A complete index series plus the diagnostics behind it."""

    points: list[IndexPoint] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    label: str = ""

    def to_frame(self) -> pd.DataFrame:
        """The series as a DataFrame, ready for charting."""
        if not self.points:
            return pd.DataFrame(
                columns=["period", "value", "matched_items", "link", "note"]
            )
        return pd.DataFrame([vars(p) for p in self.points])

    @property
    def latest(self) -> IndexPoint | None:
        return self.points[-1] if self.points else None

    @property
    def total_change_pct(self) -> float | None:
        """Percentage change from the first period to the last."""
        if len(self.points) < 2:
            return None
        first, last = self.points[0].value, self.points[-1].value
        return (last / first - 1.0) * 100.0 if first else None


def _period_sort_key(period: str) -> tuple[int, int]:
    """Sort 'YYYY-MM' chronologically."""
    year, month = period.split("-")
    return int(year), int(month)


def build_index(
    observations: pd.DataFrame,
    label: str = "Menu index",
) -> IndexResult:
    """
    Build a chained, matched-item index from observed prices.

    `observations` needs the columns produced by database.load_observations:
    period, item_id, price, plus the restaurant and category fields.

    The steps:
      1. Average any duplicate readings of one item within a period.
      2. For each neighbouring pair of periods, keep the items present in both.
      3. Drop implausible jumps, which are nearly always parsing mistakes.
      4. Compute that pair's link as (sum of new prices) / (sum of old prices).
      5. Multiply the links together, starting at 100.
    """
    settings = load_settings()["index"]
    base_value = float(settings["base_value"])
    max_change = float(settings["max_monthly_change_pct"])
    min_periods = int(settings["min_periods_per_item"])

    result = IndexResult(label=label)
    if observations.empty:
        return result

    # Step 1: one price per item per period. A mid-month re-run or a menu
    # listing an item twice would otherwise let it count more than once.
    priced = (
        observations.groupby(["period", "item_id"], as_index=False)["price"]
        .mean()
    )

    # The single-period case is settled BEFORE any item filtering.
    #
    # Filtering first would drop every item, since nothing can appear in two
    # periods when only one exists, and the function would return an empty
    # series where the honest answer is "here is the starting point, with
    # nothing yet to compare it against".
    periods = sorted(priced["period"].unique(), key=_period_sort_key)
    if len(periods) < 2:
        result.points.append(
            IndexPoint(periods[0], base_value, 0, 1.0, "base period")
        )
        return result

    # Keep only items with enough history to show a change at all.
    appearances = priced.groupby("item_id")["period"].nunique()
    eligible = appearances[appearances >= min_periods].index
    priced = priced[priced["item_id"].isin(eligible)]

    if priced.empty:
        return result

    periods = sorted(priced["period"].unique(), key=_period_sort_key)
    if len(periods) < 2:
        result.points.append(
            IndexPoint(periods[0], base_value, 0, 1.0, "base period")
        )
        return result

    # Prices laid out as a grid: rows are items, columns are periods. This
    # makes "items present in both of two periods" a simple pair of lookups.
    grid = priced.pivot(index="item_id", columns="period", values="price")

    result.points.append(
        IndexPoint(periods[0], base_value, 0, 1.0, "base period")
    )

    running = base_value
    for previous, current in zip(periods, periods[1:]):
        pair = grid[[previous, current]].dropna()

        if pair.empty:
            result.points.append(
                IndexPoint(current, running, 0, 1.0,
                           "no items priced in both months; index carried forward")
            )
            continue

        # Step 3: drop moves too large to be believable. A 400% jump is
        # almost always a parse error or a bad name match, and one of them
        # would swamp every real price move in the same month.
        change_pct = (pair[current] / pair[previous] - 1.0) * 100.0
        implausible = change_pct.abs() > max_change
        for item_id in pair.index[implausible]:
            result.excluded.append({
                "period": current,
                "item_id": int(item_id),
                "from": round(float(pair.loc[item_id, previous]), 2),
                "to": round(float(pair.loc[item_id, current]), 2),
                "change_pct": round(float(change_pct.loc[item_id]), 1),
                "reason": f"move above the {max_change:.0f}% plausibility limit",
            })
        pair = pair[~implausible]

        if pair.empty:
            result.points.append(
                IndexPoint(current, running, 0, 1.0,
                           "every matched item was excluded as implausible")
            )
            continue

        # Step 4: the link for this pair of months.
        link = float(pair[current].sum() / pair[previous].sum())

        # Step 5: chain it on.
        running *= link
        result.points.append(
            IndexPoint(current, round(running, 2), int(len(pair)), round(link, 6))
        )

    return result


def build_category_indices(observations: pd.DataFrame) -> dict[str, IndexResult]:
    """
    One index per restaurant category.

    Built with the same function on a filtered slice, so a fast-food index and
    the overall index can never disagree about method. Categories with too
    little data are left out rather than published thin.
    """
    settings = load_settings()["index"]
    min_items = int(settings["min_items_per_restaurant"])

    results: dict[str, IndexResult] = {}
    for category in sorted(observations["category"].dropna().unique()):
        slice_ = observations[observations["category"] == category]

        # A category resting on one restaurant is that restaurant's pricing
        # decisions rather than a category trend, so it is skipped.
        if slice_["restaurant_id"].nunique() < 2:
            continue
        if slice_["item_id"].nunique() < min_items:
            continue

        results[category] = build_index(
            slice_, label=category.replace("_", " ").capitalize()
        )

    return results


def item_price_changes(
    observations: pd.DataFrame,
    top_n: int = 15,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    The largest increases and decreases for individual items.

    Compares each item's FIRST and LAST observed price, so it answers "what
    happened to this item over the whole window". Items seen in only one
    period are skipped, having no change to report.

    Returns (increases, decreases), each sorted by size of move.
    """
    if observations.empty:
        empty = pd.DataFrame()
        return empty, empty

    priced = (
        observations.groupby(["item_id", "period"], as_index=False)
        .agg(
            price=("price", "mean"),
            canonical_name=("canonical_name", "first"),
            restaurant_name=("restaurant_name", "first"),
            category=("category", "first"),
        )
    )
    priced["_sort"] = priced["period"].map(_period_sort_key)
    priced = priced.sort_values(["item_id", "_sort"])

    rows = []
    for item_id, group in priced.groupby("item_id"):
        if len(group) < 2:
            continue
        first, last = group.iloc[0], group.iloc[-1]
        if first["price"] <= 0:
            continue
        rows.append({
            "item": first["canonical_name"],
            "restaurant": first["restaurant_name"],
            "category": first["category"],
            "first_period": first["period"],
            "last_period": last["period"],
            "first_price": round(float(first["price"]), 2),
            "last_price": round(float(last["price"]), 2),
            "change_pct": round((float(last["price"]) / float(first["price"]) - 1) * 100, 1),
            "change_dollars": round(float(last["price"]) - float(first["price"]), 2),
        })

    if not rows:
        empty = pd.DataFrame()
        return empty, empty

    changes = pd.DataFrame(rows)
    increases = changes[changes["change_pct"] > 0].nlargest(top_n, "change_pct")
    decreases = changes[changes["change_pct"] < 0].nsmallest(top_n, "change_pct")
    return increases.reset_index(drop=True), decreases.reset_index(drop=True)


def basket_summary(observations: pd.DataFrame) -> dict:
    """Facts about the basket, shown in the methodology section."""
    if observations.empty:
        return {}

    settings = load_settings()["index"]
    min_periods = int(settings["min_periods_per_item"])

    appearances = observations.groupby("item_id")["period"].nunique()
    in_basket = appearances[appearances >= min_periods]

    return {
        "total_items": int(observations["item_id"].nunique()),
        "basket_items": int(len(in_basket)),
        "restaurants": int(observations["restaurant_id"].nunique()),
        "periods": int(observations["period"].nunique()),
        "observations": int(len(observations)),
        "median_periods_per_item": float(appearances.median()),
    }


def rebase(frame: pd.DataFrame, value_column: str, base_period: str) -> pd.DataFrame:
    """
    Rescale a series so it equals 100 in `base_period`.

    Needed to put this project's index and the CPI on one chart. The CPI sits
    near 390 on a 1982-84 base while the menu index starts at 100, so plotting
    them raw would put one line flat against the axis. Rebasing both to the
    same month compares their SHAPES, which is the honest comparison: the
    question is whether Reno menu prices moved like the national average, and
    the answer lies in the rate of change.
    """
    frame = frame.copy()
    base_rows = frame[frame["period"] == base_period]
    if base_rows.empty or float(base_rows.iloc[0][value_column]) == 0:
        return frame
    base = float(base_rows.iloc[0][value_column])
    frame[value_column] = frame[value_column] / base * 100.0
    return frame
