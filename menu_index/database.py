"""
database.py - the SQLite store, and every query that reads from it.

WHY SQLITE
It is a single file with no server to install or run, it ships inside Python,
and Streamlit Community Cloud can read it straight from the repository. For a
dataset of a few thousand prices that is the right size of tool.

THE SHAPE OF THE DATA, AND WHY

    restaurants   one row per restaurant, mirroring restaurants.yaml
    items         one row per DISTINCT MENU ITEM at a restaurant
    observations  one row per price seen, for one item, in one period

The important decision is that `items` and `observations` are separate tables.

A first attempt at this would store one row per scraped line: restaurant,
item name, price, date. That works right up until a menu renames "Classic
Cheeseburger" to "Cheeseburger". Grouping by name then treats those as two
different items, each with half a history, and the price change between them
disappears from the index entirely.

Separating the tables fixes that. An `item` is a lasting thing with an
identity that survives a rename. An `observation` records what a menu said on
one day, including the exact `observed_name` printed at the time. When a name
changes, normalize.py links the new text to the existing item, and the series
continues unbroken. Keeping `observed_name` on every row means a wrong match
can be found and undone later, because the original text was never discarded.

THE `dataset` COLUMN
Every observation is labelled "real" or "simulated". Every read filters on it.
This is what keeps invented demo numbers out of any real result, and it is
enforced here at the storage layer so no calling code has to remember.

WHAT IS DELIBERATELY ABSENT
Raw HTML is never stored in this database. Pages are cached as files under
data/cache/ purely so a re-run avoids re-fetching, and .gitignore excludes
that folder. What gets committed is item names and prices, which is the
aggregate this project is about.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, Restaurant, load_restaurants

DB_PATH = DATA_DIR / "menu_prices.sqlite"

REAL = "real"
SIMULATED = "simulated"

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    url         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  TEXT NOT NULL REFERENCES restaurants(id),
    -- The name this item is filed under. Later observations with different
    -- wording still attach here once normalize.py matches them.
    canonical_name TEXT NOT NULL,
    -- Cleaned form used for matching. Stored rather than recomputed so the
    -- matching rules can change without silently rewriting history.
    match_key      TEXT NOT NULL,
    -- Portion size, when the menu states one: (16, 'oz'), (2, 'pc').
    -- Held apart from the name so that a size change is visible as a size
    -- change instead of hiding inside a price rise. See the shrinkflation
    -- note in indexer.py.
    size_value     REAL,
    size_unit      TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    UNIQUE (restaurant_id, match_key, size_value, size_unit)
);

CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES items(id),
    -- Calendar month as 'YYYY-MM'. The index works in monthly periods, and
    -- storing the period directly keeps grouping simple and unambiguous.
    period        TEXT NOT NULL,
    -- Money is stored in whole CENTS as an integer.
    -- Floating point cannot represent 0.10 exactly, so repeated addition of
    -- dollar floats drifts. Integer cents are exact, and the conversion to
    -- dollars happens once, at display time.
    price_cents   INTEGER NOT NULL,
    -- Exactly what the menu said on the day, kept so a bad name match can be
    -- traced back and corrected.
    observed_name TEXT NOT NULL,
    scraped_at    TEXT NOT NULL,
    dataset       TEXT NOT NULL CHECK (dataset IN ('real', 'simulated')),
    -- One price per item per period per dataset. A re-run in the same month
    -- replaces the earlier reading rather than adding a duplicate.
    UNIQUE (item_id, period, dataset)
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id TEXT NOT NULL,
    ran_at        TEXT NOT NULL,
    status        TEXT NOT NULL,   -- fetched | skipped | error
    reason        TEXT NOT NULL,
    items_found   INTEGER NOT NULL DEFAULT 0
);

-- Indexes on the columns the dashboard filters by most.
CREATE INDEX IF NOT EXISTS idx_obs_period  ON observations(period);
CREATE INDEX IF NOT EXISTS idx_obs_dataset ON observations(dataset);
CREATE INDEX IF NOT EXISTS idx_items_rest  ON items(restaurant_id);
"""


@dataclass
class ParsedItem:
    """One item read off a menu page, before it reaches the database."""

    observed_name: str
    price_cents: int
    size_value: float | None = None
    size_unit: str | None = None


@contextmanager
def connect(db_path: Path | None = None):
    """
    Open the database, creating the schema when needed.

    Used as a context manager so the connection always closes and the
    transaction always resolves, including when an exception is raised
    partway through a scrape.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    # Return rows that can be addressed by column name, which keeps the
    # calling code readable as the schema grows.
    connection.row_factory = sqlite3.Row
    # SQLite leaves foreign keys off by default for backward compatibility.
    # Turning them on means a bad item_id is rejected at write time.
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.executescript(SCHEMA)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sync_restaurants(connection: sqlite3.Connection) -> None:
    """
    Copy restaurants.yaml into the database.

    The YAML stays the source of truth. This copy exists so that historical
    rows still resolve to a name and category after a restaurant is removed
    from the config, which keeps old charts readable.
    """
    for restaurant in load_restaurants():
        connection.execute(
            """
            INSERT INTO restaurants (id, name, category, url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                category = excluded.category,
                url = excluded.url
            """,
            (restaurant.id, restaurant.name, restaurant.category, restaurant.url),
        )


def upsert_item(
    connection: sqlite3.Connection,
    restaurant_id: str,
    canonical_name: str,
    match_key: str,
    size_value: float | None,
    size_unit: str | None,
    seen_on: str,
) -> int:
    """Find or create an item, and return its id."""
    row = connection.execute(
        """
        SELECT id FROM items
        WHERE restaurant_id = ? AND match_key = ?
          AND size_value IS ? AND size_unit IS ?
        """,
        (restaurant_id, match_key, size_value, size_unit),
    ).fetchone()

    if row:
        connection.execute(
            "UPDATE items SET last_seen = ? WHERE id = ?", (seen_on, row["id"])
        )
        return int(row["id"])

    cursor = connection.execute(
        """
        INSERT INTO items
            (restaurant_id, canonical_name, match_key,
             size_value, size_unit, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (restaurant_id, canonical_name, match_key,
         size_value, size_unit, seen_on, seen_on),
    )
    return int(cursor.lastrowid)


def record_observation(
    connection: sqlite3.Connection,
    item_id: int,
    period: str,
    price_cents: int,
    observed_name: str,
    dataset: str,
    scraped_at: str | None = None,
) -> None:
    """
    Store one price reading.

    Re-running in the same month overwrites that month's reading rather than
    adding a second one, which is what the UNIQUE constraint expresses. Two
    readings in one month would otherwise both feed the index and quietly
    double that item's weight.
    """
    connection.execute(
        """
        INSERT INTO observations
            (item_id, period, price_cents, observed_name, scraped_at, dataset)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id, period, dataset) DO UPDATE SET
            price_cents   = excluded.price_cents,
            observed_name = excluded.observed_name,
            scraped_at    = excluded.scraped_at
        """,
        (item_id, period, price_cents, observed_name,
         scraped_at or dt.datetime.now().isoformat(timespec="seconds"), dataset),
    )


def log_scrape(
    connection: sqlite3.Connection,
    restaurant_id: str,
    status: str,
    reason: str,
    items_found: int = 0,
) -> None:
    """Record what happened for one restaurant on one run."""
    connection.execute(
        """
        INSERT INTO scrape_log (restaurant_id, ran_at, status, reason, items_found)
        VALUES (?, ?, ?, ?, ?)
        """,
        (restaurant_id, dt.datetime.now().isoformat(timespec="seconds"),
         status, reason, items_found),
    )


def existing_items(
    connection: sqlite3.Connection, restaurant_id: str
) -> list[sqlite3.Row]:
    """Every item already known for a restaurant, for name matching."""
    return connection.execute(
        "SELECT id, canonical_name, match_key, size_value, size_unit "
        "FROM items WHERE restaurant_id = ?",
        (restaurant_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Reads used by the index and the dashboard.
#
# Each one takes `dataset` and passes it into the SQL. Making it a required
# argument rather than one with a default means a caller has to decide which
# data it wants, which is how the real and simulated sets stay apart.
# ---------------------------------------------------------------------------

def load_observations(dataset: str, db_path: Path | None = None) -> pd.DataFrame:
    """
    Every observation in one dataset, joined to its item and restaurant.

    Returns columns:
        period, item_id, canonical_name, observed_name, price (dollars),
        size_value, size_unit, restaurant_id, restaurant_name, category
    """
    with connect(db_path) as connection:
        frame = pd.read_sql_query(
            """
            SELECT
                o.period            AS period,
                o.item_id           AS item_id,
                i.canonical_name    AS canonical_name,
                o.observed_name     AS observed_name,
                o.price_cents       AS price_cents,
                i.size_value        AS size_value,
                i.size_unit         AS size_unit,
                r.id                AS restaurant_id,
                r.name              AS restaurant_name,
                r.category          AS category
            FROM observations o
            JOIN items       i ON i.id = o.item_id
            JOIN restaurants r ON r.id = i.restaurant_id
            WHERE o.dataset = ?
            ORDER BY o.period, r.name, i.canonical_name
            """,
            connection,
            params=(dataset,),
        )

    # Convert to dollars once, here, so no other file repeats the division and
    # risks getting it wrong.
    frame["price"] = frame["price_cents"] / 100.0
    return frame


def available_datasets(db_path: Path | None = None) -> list[str]:
    """Which datasets hold any data, so the dashboard can offer the right ones."""
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT DISTINCT dataset FROM observations"
        ).fetchall()
    return sorted(row["dataset"] for row in rows)


def dataset_summary(dataset: str, db_path: Path | None = None) -> dict:
    """Counts shown in the dashboard header."""
    with connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*)                       AS observations,
                COUNT(DISTINCT o.item_id)      AS items,
                COUNT(DISTINCT i.restaurant_id) AS restaurants,
                COUNT(DISTINCT o.period)       AS periods,
                MIN(o.period)                  AS first_period,
                MAX(o.period)                  AS last_period
            FROM observations o
            JOIN items i ON i.id = o.item_id
            WHERE o.dataset = ?
            """,
            (dataset,),
        ).fetchone()
    return dict(row) if row else {}


def recent_scrape_log(limit: int = 60, db_path: Path | None = None) -> pd.DataFrame:
    """The latest scrape outcomes, for the dashboard's data-quality panel."""
    with connect(db_path) as connection:
        return pd.read_sql_query(
            """
            SELECT s.ran_at, s.restaurant_id, r.name AS restaurant_name,
                   s.status, s.reason, s.items_found
            FROM scrape_log s
            LEFT JOIN restaurants r ON r.id = s.restaurant_id
            ORDER BY s.ran_at DESC
            LIMIT ?
            """,
            connection,
            params=(limit,),
        )


def clear_dataset(dataset: str, db_path: Path | None = None) -> int:
    """
    Delete every observation in one dataset, and any item left with none.

    Used by the seed script so that regenerating demo data replaces it instead
    of stacking a second copy on top. Restricting it to a single dataset means
    reseeding the demo data can never touch real observations.
    """
    with connect(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM observations WHERE dataset = ?", (dataset,)
        )
        removed = cursor.rowcount
        connection.execute(
            "DELETE FROM items WHERE id NOT IN (SELECT item_id FROM observations)"
        )
    return removed
