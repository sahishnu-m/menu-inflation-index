"""
config.py - reads config/restaurants.yaml and config/settings.yaml.

DESIGN CHOICE: the YAML becomes dataclasses here, and this is the only file
that knows what the YAML looks like.

Passing raw dictionaries around would mean a typo such as `r["catagory"]`
surviving all the way to the dashboard before failing, with an error that
points at the wrong place. Converting once, at startup, turns a typo into an
immediate and specific complaint. It also means an editor can autocomplete
`restaurant.category`. Adding a field to restaurants.yaml is a change to this
file alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Derived from this file's own location, so the app works from any working
# directory. __file__ is this module, .parent is menu_index/, .parent again is
# the repository root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

VALID_CATEGORIES = ("fast_food", "casual", "mid_range")
VALID_RENDERERS = ("static", "javascript")


@dataclass(frozen=True)
class Selectors:
    """The three CSS selectors needed to read one menu page."""

    item: str  # the repeating block that wraps a single menu item
    name: str  # searched inside `item`
    price: str  # searched inside `item`


@dataclass(frozen=True)
class Restaurant:
    """One restaurant from restaurants.yaml."""

    id: str
    name: str
    category: str
    url: str
    renderer: str  # "static" or "javascript"
    verified: bool
    selectors: Selectors
    notes: str = ""

    @property
    def needs_browser(self) -> bool:
        """True when this page builds its menu with JavaScript."""
        return self.renderer == "javascript"

    @property
    def category_label(self) -> str:
        """"fast_food" as "Fast food", for display."""
        return self.category.replace("_", " ").capitalize()


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML file, with a clear message when it is missing."""
    if not path.exists():
        raise FileNotFoundError(
            f"Could not find {path}. Both config/restaurants.yaml and "
            "config/settings.yaml are required."
        )
    # safe_load rather than load: safe_load cannot execute Python embedded in
    # a YAML file. For reading configuration it is simply the correct default.
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@lru_cache(maxsize=1)
def load_settings() -> dict[str, Any]:
    """
    Load config/settings.yaml as a nested dictionary.

    This one stays a plain dictionary while restaurants become dataclasses.
    The reason is that settings get read in many places, each wanting a
    different slice, and the shape changes whenever a tuning knob is added.
    Freezing that shape into a dataclass would mean editing Python every time
    you wanted a new number, which is the friction this project avoids.

    @lru_cache reads the file once per process. Streamlit re-runs the whole
    script on every interaction, so an uncached read here would re-parse the
    file on every click. In a companion project, a missing cache on a function
    exactly like this one turned a two-second job into a four-minute one.
    """
    return _load_yaml(CONFIG_DIR / "settings.yaml")


@lru_cache(maxsize=1)
def load_restaurants() -> tuple[Restaurant, ...]:
    """Load and validate config/restaurants.yaml."""
    raw = _load_yaml(CONFIG_DIR / "restaurants.yaml")
    restaurants: list[Restaurant] = []

    for entry in raw.get("restaurants", []):
        entry_id = entry.get("id", "<missing id>")

        # Validate the two fields that have a fixed set of allowed values.
        # Catching these here means a typo produces one clear sentence rather
        # than a mysteriously empty chart three files later.
        category = entry.get("category")
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"Restaurant {entry_id!r} has category {category!r}. "
                f"Valid categories: {', '.join(VALID_CATEGORIES)}."
            )

        renderer = entry.get("renderer", "static")
        if renderer not in VALID_RENDERERS:
            raise ValueError(
                f"Restaurant {entry_id!r} has renderer {renderer!r}. "
                f"Valid renderers: {', '.join(VALID_RENDERERS)}."
            )

        selectors = entry.get("selectors") or {}
        missing = [k for k in ("item", "name", "price") if not selectors.get(k)]
        if missing:
            raise ValueError(
                f"Restaurant {entry_id!r} is missing selector(s): "
                f"{', '.join(missing)}."
            )

        restaurants.append(
            Restaurant(
                id=entry["id"],
                name=entry["name"],
                category=category,
                url=entry["url"],
                renderer=renderer,
                verified=bool(entry.get("verified", False)),
                selectors=Selectors(
                    item=selectors["item"],
                    name=selectors["name"],
                    price=selectors["price"],
                ),
                notes=(entry.get("notes") or "").strip(),
            )
        )

    if not restaurants:
        raise ValueError("restaurants.yaml lists no restaurants.")

    # Catch the copy-paste mistake of duplicating a block and leaving the id
    # alone, which would make one restaurant silently shadow another.
    ids = [r.id for r in restaurants]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"Duplicate restaurant id(s): {duplicates}")

    return tuple(restaurants)


def find_restaurant(restaurant_id: str) -> Restaurant:
    """Look up one restaurant by id, listing the valid ids when it is absent."""
    for restaurant in load_restaurants():
        if restaurant.id == restaurant_id:
            return restaurant
    known = ", ".join(r.id for r in load_restaurants())
    raise KeyError(f"No restaurant with id {restaurant_id!r}. Known ids: {known}")


def category_labels() -> dict[str, str]:
    """The category descriptions from restaurants.yaml, for the dashboard."""
    raw = _load_yaml(CONFIG_DIR / "restaurants.yaml")
    return raw.get("meta", {}).get("categories", {})


def user_agent() -> str:
    """The User-Agent string sent with every request."""
    return " ".join(load_settings()["scraping"]["user_agent"].split())
