"""
The web app.

Run it locally:
    streamlit run app.py

Streamlit reruns this whole script from top to bottom every time you click
something. The @st.cache_data decorators make sure the slow parts (reading the
database, calling the BLS API, building the index) only happen once.

Look and feel comes from .streamlit/config.toml, so no CSS is injected here
beyond the two small st.html blocks that draw the headline figures and the
key/value rows.
"""

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from menu_index.bls import BLSUnavailable, cache_age_hours, cpi_change_pct, fetch_cpi
from menu_index.config import category_labels, load_restaurants, load_settings
from menu_index.database import (
    REAL,
    SIMULATED,
    available_datasets,
    dataset_summary,
    load_observations,
    recent_scrape_log,
)
from menu_index.indexer import (
    basket_summary,
    build_category_indices,
    build_index,
    item_price_changes,
    rebase,
)

st.set_page_config(page_title="Menu Inflation Index", layout="centered")

MUTED = "#52514e"
INK = "#0b0b0b"
BLUE = "#2a78d6"    # this project's index
ORANGE = "#eb6834"  # the official CPI
GREEN = "#1baf7a"

CATEGORY_COLORS = {"fast_food": BLUE, "casual": ORANGE, "mid_range": GREEN}


# ---------------------------------------------------------------------------
# Loading (cached)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600, show_spinner=False)
def observations_for(dataset: str) -> pd.DataFrame:
    return load_observations(dataset)


@st.cache_data(ttl=600, show_spinner=False)
def index_for(dataset: str) -> tuple:
    """The overall index and the per-category indices, built once per dataset."""
    frame = observations_for(dataset)
    return build_index(frame, "Menu index"), build_category_indices(frame)


@st.cache_data(ttl=3600, show_spinner=False)
def cpi_series() -> pd.DataFrame:
    return fetch_cpi()


# ---------------------------------------------------------------------------
# Small building blocks
# ---------------------------------------------------------------------------

def headline(left_label: str, left_value: str, right_label: str,
             right_value: str, right_color: str, note: str) -> None:
    """The two big numbers at the top, side by side with a note beneath."""
    st.html(f"""
    <div style="font-family:inherit;margin:0.5rem 0 0.25rem">
      <div style="display:flex;justify-content:space-between;gap:1rem;align-items:flex-end">
        <div style="min-width:0">
          <div style="color:{MUTED};font-size:0.9rem">{left_label}</div>
          <div style="color:{INK};font-size:2.4rem;font-weight:700;line-height:1.1">{left_value}</div>
        </div>
        <div style="min-width:0;text-align:right">
          <div style="color:{MUTED};font-size:0.9rem">{right_label}</div>
          <div style="color:{right_color};font-size:2.4rem;font-weight:700;line-height:1.1">{right_value}</div>
        </div>
      </div>
      <div style="color:{MUTED};font-size:0.9rem;margin-top:0.5rem">{note}</div>
    </div>""")


def kv_rows(pairs: list[tuple[str, str]]) -> None:
    """A two-column list used for summaries and logs."""
    body = "".join(
        f'<div style="display:flex;justify-content:space-between;gap:1rem;'
        f'padding:0.4rem 0;border-bottom:1px solid #e6e5e0">'
        f'<span style="color:{INK}">{k}</span>'
        f'<span style="color:{MUTED};text-align:right">{v}</span></div>'
        for k, v in pairs
    )
    st.html(f'<div style="font-family:inherit;font-size:0.9rem">{body}</div>')


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

restaurants = load_restaurants()
settings = load_settings()

st.title("Menu Inflation Index")

# ---------------------------------------------------------------------------
# Dataset selection.
#
# The most important decision on the page, settled before anything else is
# drawn. Real and simulated observations live in the same database under
# separate labels and are never combined, so a persistent banner sits at the
# very top whenever what follows is simulated. Its numbers (how many months of
# real data exist, and when real collection began) are read from the database
# on every load rather than written into this file, so they cannot go stale.
# ---------------------------------------------------------------------------

datasets = available_datasets()
if not datasets:
    st.error("The database holds no observations yet.")
    st.markdown(
        "Generate simulated demo data with:\n\n"
        "```\npython scripts/seed_demo_data.py\n```\n\n"
        "Or collect real prices with:\n\n"
        "```\npython scripts/run_scrape.py\n```"
    )
    st.stop()

real_summary = dataset_summary(REAL)
real_months = real_summary.get("periods") or 0

default = settings["data"]["default_dataset"]
if REAL in datasets and real_months >= 2:
    default = REAL  # real data takes over as soon as there is enough to plot

dataset = st.radio(
    "Data source",
    datasets,
    index=datasets.index(default) if default in datasets else 0,
    format_func=lambda d: {
        REAL: "Real collected prices",
        SIMULATED: "Simulated demo data",
    }.get(d, d),
    horizontal=True,
    help="Switches to real collected prices automatically once two or more "
         "months of real data exist. Until then only simulated demo data is "
         "available to select.",
)

if dataset == SIMULATED:
    if real_months == 0:
        real_note = "No real prices have been collected yet."
    elif real_months == 1:
        started = real_summary.get("first_period", "an unknown month")
        real_note = f"Real collection began in {started} and has produced 1 month of data so far."
    else:
        started = real_summary.get("first_period", "an unknown month")
        real_note = (
            f"Real collection began in {started} and has produced "
            f"{real_months} months of data so far."
        )
    st.warning(
        "**Showing simulated demo data.** These numbers were generated by "
        "`scripts/seed_demo_data.py` to give the dashboard something to plot "
        "before real collection has enough history. They describe nothing "
        f"about actual Reno prices. {real_note} Real and simulated "
        "observations are stored under separate labels in the database and "
        "are never combined.",
        icon=None,
    )

st.markdown(
    f"""
This is an economics project that measures what eating out costs in **Reno and
Sparks**, and sets that against the official national figure.

Menu prices from **{len(restaurants)} restaurants** across three price tiers are
collected monthly, matched item by item, and turned into a **price index**: a
fixed basket of the same dishes, priced again each month, so the number moves
only when prices move. That index is then compared against the Bureau of Labor
Statistics series for **food away from home** (`CUUR0000SEFV`), which is the
government's measure of restaurant prices nationally.

Price data: scraped from public restaurant menu pages, with robots.txt treated
as binding. CPI data: [BLS Public Data API](https://www.bls.gov/developers/),
free and in the public domain.
"""
)

observations = observations_for(dataset)
if observations.empty:
    st.info("That dataset holds no observations yet.")
    st.stop()

index_result, category_results = index_for(dataset)
index_frame = index_result.to_frame()
summary = dataset_summary(dataset)

# ---------------------------------------------------------------------------
# Part I: the headline comparison
# ---------------------------------------------------------------------------

st.header("Part I: Menu prices against the national CPI", divider="gray")

periods = index_frame["period"].tolist()
cpi_change = None
cpi_frame = pd.DataFrame()
try:
    cpi_frame = cpi_series()
    cpi_change = cpi_change_pct(cpi_frame, periods)
except BLSUnavailable as exc:
    st.caption(f"The CPI series is unavailable right now: {exc}")

own_change = index_result.total_change_pct or 0.0
span = f"{periods[0]} to {periods[-1]}" if len(periods) > 1 else periods[0]

if cpi_change is not None:
    gap = own_change - cpi_change
    direction = "faster than" if gap > 0 else "slower than"
    note = (
        f"Over {span}, this basket moved {abs(gap):.1f} percentage points "
        f"{direction} the national CPI for food away from home."
    )
    headline(
        "Reno menu basket", f"{own_change:+.1f}%",
        "National CPI", f"{cpi_change:+.1f}%", ORANGE, note,
    )
else:
    headline(
        "Reno menu basket", f"{own_change:+.1f}%",
        "Index level", f"{index_frame['value'].iloc[-1]:.1f}", BLUE,
        f"Change over {span}.",
    )

# Both series rebased to the first shared month, which is what makes them
# comparable on one pair of axes. See indexer.rebase for why.
chart_rows = [
    {"period": row["period"], "value": row["value"], "series": "Reno menu index"}
    for _, row in index_frame.iterrows()
]

if not cpi_frame.empty:
    overlap = cpi_frame[cpi_frame["period"].isin(periods)]
    if len(overlap) >= 2:
        rebased = rebase(overlap, "cpi", overlap.iloc[0]["period"])
        chart_rows += [
            {"period": row["period"], "value": row["cpi"],
             "series": "CPI: food away from home"}
            for _, row in rebased.iterrows()
        ]

comparison = pd.DataFrame(chart_rows)

line = (
    alt.Chart(comparison)
    .mark_line(point=True, strokeWidth=2.5)
    .encode(
        x=alt.X("period:N", title=None,
                axis=alt.Axis(labelAngle=-45, ticks=False, domain=False)),
        y=alt.Y("value:Q", title="Index (first month = 100)",
                scale=alt.Scale(zero=False)),
        color=alt.Color(
            "series:N", title=None,
            scale=alt.Scale(
                domain=["Reno menu index", "CPI: food away from home"],
                range=[BLUE, ORANGE],
            ),
            legend=alt.Legend(orient="top", symbolType="stroke"),
        ),
        tooltip=[
            alt.Tooltip("period:N", title="Month"),
            alt.Tooltip("series:N", title="Series"),
            alt.Tooltip("value:Q", title="Index", format=".2f"),
        ],
    )
    .properties(height=340)
)
st.altair_chart(line, width="stretch")
st.caption(
    "Both series are set to 100 in the first shared month. The CPI is published "
    "on a 1982-84 base and sits near 400, so rescaling is what lets the two be "
    "read against each other. What the chart compares is the rate of change."
)

c1, c2, c3 = st.columns(3)
c1.metric("Months of data", summary.get("observations") and len(periods) or 0)
c2.metric("Items in the basket", basket_summary(observations).get("basket_items", 0))
c3.metric("Restaurants", summary.get("restaurants", 0))

# ---------------------------------------------------------------------------
# Part II: by category
# ---------------------------------------------------------------------------

st.header("Part II: Which price tier moved most?", divider="gray")
st.markdown(
    """
Each tier gets its own index, built by the same function on a filtered slice,
so the three can never disagree with the overall figure about method. A tier
resting on a single restaurant is left out, since that would describe one
restaurant's pricing decisions instead of a trend.
"""
)

if category_results:
    labels = category_labels()
    rows = []
    for category, result in category_results.items():
        for point in result.points:
            rows.append({
                "period": point.period,
                "value": point.value,
                "category": category.replace("_", " ").capitalize(),
                "category_key": category,
            })
    category_frame = pd.DataFrame(rows)

    category_chart = (
        alt.Chart(category_frame)
        .mark_line(point=True, strokeWidth=2.2)
        .encode(
            x=alt.X("period:N", title=None,
                    axis=alt.Axis(labelAngle=-45, ticks=False, domain=False)),
            y=alt.Y("value:Q", title="Index (first month = 100)",
                    scale=alt.Scale(zero=False)),
            color=alt.Color(
                "category:N", title=None,
                scale=alt.Scale(
                    domain=[c.replace("_", " ").capitalize() for c in CATEGORY_COLORS],
                    range=list(CATEGORY_COLORS.values()),
                ),
                legend=alt.Legend(orient="top", symbolType="stroke"),
            ),
            tooltip=[
                alt.Tooltip("period:N", title="Month"),
                alt.Tooltip("category:N", title="Tier"),
                alt.Tooltip("value:Q", title="Index", format=".2f"),
            ],
        )
        .properties(height=320)
    )
    st.altair_chart(category_chart, width="stretch")

    st.dataframe(
        pd.DataFrame([
            {
                "Tier": category.replace("_", " ").capitalize(),
                "What it covers": labels.get(category, ""),
                "Change": round(result.total_change_pct or 0.0, 2),
                "Latest index": result.points[-1].value if result.points else None,
            }
            for category, result in category_results.items()
        ]),
        hide_index=True, width="stretch",
        column_config={
            "Change": st.column_config.NumberColumn("Change over the window", format="%.2f%%"),
            "Latest index": st.column_config.NumberColumn("Latest index", format="%.1f"),
        },
    )
else:
    st.info("Not enough data yet to build per-tier indices.")

# ---------------------------------------------------------------------------
# Part III: individual items
# ---------------------------------------------------------------------------

st.header("Part III: Which dishes moved most?", divider="gray")
st.markdown(
    """
Each item's first and last observed price, compared. These are the individual
moves that the index averages out, and they are worth reading alongside it:
a modest index can hide a handful of items that jumped sharply.
"""
)

increases, decreases = item_price_changes(observations, top_n=12)

tab_up, tab_down = st.tabs(["Largest increases", "Largest decreases"])

ITEM_COLUMNS = {
    "item": "Item",
    "restaurant": "Restaurant",
    "first_price": st.column_config.NumberColumn("First", format="$%.2f"),
    "last_price": st.column_config.NumberColumn("Latest", format="$%.2f"),
    "change_pct": st.column_config.NumberColumn("Change", format="%.1f%%"),
    "change_dollars": st.column_config.NumberColumn("Change ($)", format="$%.2f"),
}

with tab_up:
    if increases.empty:
        st.info("No price increases recorded yet.")
    else:
        st.dataframe(
            increases[list(ITEM_COLUMNS)], hide_index=True,
            width="stretch", column_config=ITEM_COLUMNS,
        )

with tab_down:
    if decreases.empty:
        st.info("No price decreases recorded yet.")
    else:
        st.dataframe(
            decreases[list(ITEM_COLUMNS)], hide_index=True,
            width="stretch", column_config=ITEM_COLUMNS,
        )

# ---------------------------------------------------------------------------
# Part IV: methodology
# ---------------------------------------------------------------------------

st.header("Part IV: How the index is built", divider="gray")

basket = basket_summary(observations)

st.markdown(
    f"""
**The average price would answer a different question.** Averaging every price
each month produces a number that moves whenever the MIX of items changes.
A steakhouse publishing its full menu in February would send that average
climbing while nothing got more expensive.

**So the basket is held fixed.** This is a **Laspeyres-style index**: a basket
chosen in one period is priced again in later periods, and every change in the
number comes from prices rather than composition. It is the family the CPI
belongs to, which is what makes the comparison in Part I reasonable.

**Items must appear in both months being compared.** An item priced this month
but absent last month would put something in the numerator that the
denominator lacks, and the ratio would measure menu growth.

**The index is chained.** Rather than comparing every month against one fixed
base forever, which shrinks the basket as dishes disappear, each month is
compared against the one before it, where the overlap is largest, and those
links are multiplied together.

Right now the basket holds **{basket.get('basket_items', 0)} items** across
**{basket.get('restaurants', 0)} restaurants** and **{basket.get('periods', 0)} months**,
from **{basket.get('observations', 0):,} price observations**.
"""
)

with st.expander("Where this method falls short"):
    st.markdown(
        f"""
Four limitations matter enough to state plainly.

**No quantity weights.** A real CPI weights each item by how much people buy,
using national expenditure surveys. This project has no such data, so every
item counts equally. A $42 steak therefore carries the same weight as a $3
soft drink, and because the index compares sums of prices, expensive dishes end
up dominating anyway. A tier full of costly entrees will look different from a
tier of cheap ones for reasons of arithmetic as much as economics.

**Small sample.** {basket.get('restaurants', 0)} restaurants in one metro area,
against the thousands of price quotes behind the official CPI. A single
restaurant reprinting its menu is visible in this index. Treat a gap against
the CPI as a question worth asking rather than a finding.

**Survivorship bias.** The basket can only hold dishes that stay on the menu.
A restaurant dropping an item it no longer wants to sell at that price removes
exactly the observation that would have shown the largest increase, which
biases the index downward by an amount nobody can measure from inside the data.

**Menu prices are list prices.** They exclude discounts, app-only deals, happy
hour, combo pricing and delivery markups. What people actually pay moves
differently, and often by more.

One further wrinkle this project does handle: **portion size**. A drink going
from 16oz to 12oz at an unchanged price is a 33% rise per ounce that a
price-only comparison would miss entirely. Sizes are stored separately from
names, so a resized item becomes its own series instead of quietly continuing
the old one.
        """
    )

with st.expander("Data collection and ethics"):
    st.markdown(
        f"""
Prices come from public menu pages, collected under rules the code enforces
rather than leaves to whoever runs it:

- **robots.txt is checked first for every site and binds.** No override exists.
- **When robots.txt cannot be read, the fetch is refused.** Confirming
  permission is what grants it.
- **One request every {settings['scraping']['rate_limit_seconds']:.0f} seconds per host.**
- **The User-Agent names the project and links to its repository.**
- **Nothing behind a login, and no evading a block.** Several large chains
  refuse automated access. Those refusals are recorded and obeyed; disguising
  the scraper as a browser would break the intent of the rule while keeping
  its letter.
- **Only prices and item names are stored.** Raw pages are cached locally to
  avoid repeat requests and are excluded from the repository.
        """
    )

    log = recent_scrape_log(limit=25)
    if not log.empty:
        st.caption("Most recent collection attempts")
        st.dataframe(
            log[["ran_at", "restaurant_name", "status", "items_found", "reason"]],
            hide_index=True, width="stretch", height=260,
            column_config={
                "ran_at": "When", "restaurant_name": "Restaurant",
                "status": "Result", "items_found": "Items", "reason": "Detail",
            },
        )

with st.expander("Excluded observations"):
    if index_result.excluded:
        st.caption(
            f"{len(index_result.excluded)} price moves were left out of the index for "
            f"exceeding the {settings['index']['max_monthly_change_pct']:.0f}% "
            "plausibility limit. A jump that large is nearly always a parsing "
            "mistake or an item wrongly matched to another, and letting one "
            "through would swamp every genuine move around it."
        )
        st.dataframe(pd.DataFrame(index_result.excluded), hide_index=True, width="stretch")
    else:
        st.caption("No observations were excluded.")

with st.expander("Restaurants tracked"):
    st.dataframe(
        pd.DataFrame([
            {
                "Restaurant": r.name,
                "Tier": r.category.replace("_", " ").capitalize(),
                "Page type": "JavaScript" if r.needs_browser else "Static HTML",
                "Selectors checked": "yes" if r.verified else "not yet",
            }
            for r in restaurants
        ]),
        hide_index=True, width="stretch", height=320,
    )
    unverified = sum(1 for r in restaurants if not r.verified)
    if unverified:
        st.caption(
            f"{unverified} of {len(restaurants)} restaurants still have unchecked "
            "CSS selectors. Instructions for verifying one are at the top of "
            "`config/restaurants.yaml`."
        )

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

age = cache_age_hours()
st.divider()
st.caption(
    "Built as a student economics project. The index rests on a small local "
    "sample and equal item weights, so it describes the dishes in its basket "
    "rather than the Reno restaurant market as a whole. CPI data from the "
    "[U.S. Bureau of Labor Statistics](https://www.bls.gov/developers/), public "
    "domain"
    + (f", cached {age:.0f} hours ago." if age is not None else ".")
)
