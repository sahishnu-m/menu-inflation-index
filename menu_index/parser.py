"""
parser.py - turns a page of HTML into a list of items and prices.

Kept apart from scraper.py because the two jobs fail in different ways and get
fixed by different people. Fetching fails because of a network, a robots rule
or a block. Parsing fails because a restaurant redesigned its page and the
selectors in restaurants.yaml now point at nothing. Separating them means the
error message says which of those happened.

HOW SELECTORS WORK HERE
Each restaurant supplies three CSS selectors:

    item   the repeating block wrapping ONE menu item
    name   searched INSIDE that block
    price  searched INSIDE that block

Scoping name and price to within each item block is what keeps a name attached
to its own price. Selecting all names and all prices from the page separately
and zipping them together would misalign the moment one item lacks a price,
and the misalignment would be silent: every later item would carry its
neighbour's price and the data would look perfectly reasonable.

THE FALLBACK PATH
When the configured `item` selector matches nothing, a generic sweep runs
instead. It looks for any element containing a currency-shaped number and
reads a nearby name. It is far less reliable than a real selector, which is
why anything it produces is marked low confidence and the item count is
reported so the mismatch is visible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .config import Restaurant
from .database import ParsedItem
from .normalize import clean_item_name, extract_size, parse_price

logger = logging.getLogger(__name__)

# An element whose text holds something shaped like a price.
PRICE_TEXT = re.compile(r"\$\s*\d{1,3}(?:\.\d{2})?")

# Text that appears alongside prices without being a menu item.
NON_ITEM_TEXT = re.compile(
    r"^(gift card|delivery|tip|tax|subtotal|total|minimum|fee)\b", re.IGNORECASE
)


@dataclass
class ParseReport:
    """What happened while parsing one page."""

    restaurant_id: str
    items: list[ParsedItem]
    used_fallback: bool
    blocks_seen: int
    notes: str

    @property
    def count(self) -> int:
        return len(self.items)


def parse_menu(restaurant: Restaurant, html: str) -> ParseReport:
    """Read every item and price from one restaurant's menu page."""
    # lxml parses faster and copes better with broken markup than Python's
    # built-in parser, and restaurant sites produce plenty of broken markup.
    soup = BeautifulSoup(html, "lxml")

    blocks = soup.select(restaurant.selectors.item)
    if blocks:
        items = _parse_with_selectors(restaurant, blocks)
        if items:
            return ParseReport(
                restaurant.id, items, False, len(blocks),
                f"{len(items)} items from {len(blocks)} blocks",
            )
        note = (f"selector '{restaurant.selectors.item}' matched {len(blocks)} "
                "blocks but no name/price pairs came out of them")
    else:
        note = f"selector '{restaurant.selectors.item}' matched nothing"

    logger.warning("%s: %s; trying the generic sweep", restaurant.id, note)
    items = _parse_generic(soup)
    return ParseReport(
        restaurant.id, items, True, len(blocks),
        f"{note}; generic sweep found {len(items)} items",
    )


def _parse_with_selectors(restaurant: Restaurant, blocks) -> list[ParsedItem]:
    """The normal path: read each configured block."""
    items: list[ParsedItem] = []

    for block in blocks:
        name_element = block.select_one(restaurant.selectors.name)
        price_element = block.select_one(restaurant.selectors.price)
        if not name_element or not price_element:
            continue

        item = _build_item(
            name_element.get_text(" ", strip=True),
            price_element.get_text(" ", strip=True),
        )
        if item:
            items.append(item)

    return _deduplicate(items)


def _parse_generic(soup: BeautifulSoup) -> list[ParsedItem]:
    """
    The fallback: find price-shaped text and read the name beside it.

    Deliberately conservative. A menu page holds prices in footers, banners
    and gift-card blurbs, so anything without a plausible name next to it is
    dropped. Returning fewer, cleaner items beats returning many dubious ones,
    since a wrong price feeds straight into the index.
    """
    items: list[ParsedItem] = []

    for element in soup.find_all(string=PRICE_TEXT):
        price_text = str(element).strip()
        parent = element.parent
        if parent is None:
            continue

        # Look for the name in the element itself, then a sibling, then the
        # parent block. That covers the common layouts: name and price in one
        # line, side by side, or stacked inside a shared wrapper.
        name_text = ""
        container = parent.parent or parent
        full_text = container.get_text(" ", strip=True)
        # Remove the price from the block text; what remains is the name.
        name_text = PRICE_TEXT.sub("", full_text).strip(" .-–—|·")

        if not name_text or len(name_text) > 90 or NON_ITEM_TEXT.match(name_text):
            continue

        item = _build_item(name_text, price_text)
        if item:
            items.append(item)

    return _deduplicate(items)


def _build_item(name_text: str, price_text: str) -> ParsedItem | None:
    """Turn a name and price pair into a ParsedItem, or None when unusable."""
    price_cents = parse_price(price_text)
    if price_cents is None:
        return None

    name = clean_item_name(name_text)
    if len(name) < 3:
        return None

    _, size_value, size_unit = extract_size(name)
    return ParsedItem(
        observed_name=name,
        price_cents=price_cents,
        size_value=size_value,
        size_unit=size_unit,
    )


def _deduplicate(items: list[ParsedItem]) -> list[ParsedItem]:
    """
    Drop repeats of the same name and price.

    Menu pages often render an item twice, once for desktop and once for a
    mobile layout, with one hidden by CSS. BeautifulSoup reads the markup and
    knows nothing about what is visible, so it sees both. Left alone, that
    item would carry double weight in the index.
    """
    seen: set[tuple[str, int]] = set()
    unique: list[ParsedItem] = []
    for item in items:
        key = (item.observed_name.lower(), item.price_cents)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
