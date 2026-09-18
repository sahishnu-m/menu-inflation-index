"""
normalize.py - decides whether a line on today's menu is the same item that
appeared last month.

THE PROBLEM THIS SOLVES
Menus are written by people, and the wording drifts:

    "Classic Cheeseburger"  ->  "Cheeseburger"
    "Caesar Salad"          ->  "Caesar  Salad "      (stray whitespace)
    "Nachos (16 oz)"        ->  "Nachos (12 oz)"      (a real size change)
    "BBQ Burger"            ->  "Bbq Burger"          (capitalisation)

Matching on exact text treats every one of these as a brand new item. The
price series then restarts, and the change between the old and new price
vanishes from the index. Since price CHANGE is the only thing this project
measures, that failure would empty out the result.

THE APPROACH: a cleaned "match key" plus fuzzy comparison.

    1. Pull any stated size out of the name and store it separately.
    2. Clean what remains: lowercase, strip punctuation, drop filler words,
       collapse whitespace.
    3. Compare against existing items with difflib, and treat anything above
       the configured threshold as the same item.

WHY SIZE IS EXTRACTED RATHER THAN CLEANED AWAY
A 16oz drink becoming a 12oz drink at the same price is a 33% price rise per
ounce, and it is one of the most common ways restaurant prices move without
appearing to. Keeping size as its own field lets the index treat a resized
item as a distinct series, so a quiet shrink shows up instead of hiding.

WHY difflib RATHER THAN A FUZZY-MATCHING LIBRARY
difflib ships with Python, so the project gains no dependency, and its
SequenceMatcher ratio is easy to explain: roughly, the proportion of
characters the two strings share in order. A dedicated library would be
faster on very large datasets, which is a problem this project does not have.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .config import load_settings

# Words that add nothing to identity. Removing them lets "The Classic Burger"
# and "Classic Burger" land on the same key.
FILLER_WORDS = {
    "the", "a", "an", "our", "house", "classic", "signature", "famous",
    "original", "fresh", "new", "special", "style", "served", "with",
}

# Units recognised in a menu name, mapped to a single canonical spelling so
# that "oz", "ounce" and "ounces" all compare equal.
UNIT_ALIASES = {
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "pc": "pc", "pcs": "pc", "piece": "pc", "pieces": "pc",
    "ct": "pc", "count": "pc",
    "inch": "in", "in": "in", '"': "in",
    "g": "g", "gram": "g", "grams": "g",
    "ml": "ml", "l": "l", "liter": "l", "litre": "l",
}

# Matches a number followed by a unit: "16 oz", "12oz", "6-piece", "2 pc".
SIZE_PATTERN = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*[-\s]?\s*"
    r"(oz|ounces?|lbs?|pounds?|pcs?|pieces?|ct|count|inch|in|g|grams?|ml|l)\b",
    re.IGNORECASE,
)


def extract_size(name: str) -> tuple[str, float | None, str | None]:
    """
    Split a size out of a menu name.

    Returns (name without the size, size value, canonical unit).

        "Nachos (16 oz)"  ->  ("Nachos ()", 16.0, "oz")
        "6-Piece Wings"   ->  (" Wings",     6.0, "pc")
        "Cheeseburger"    ->  ("Cheeseburger", None, None)

    Only the FIRST size found is used. A name such as "12 oz steak with 8 oz
    fries" describes one item whose headline size is the first figure, and
    trying to be cleverer here would produce inconsistent keys.
    """
    match = SIZE_PATTERN.search(name)
    if not match:
        return name, None, None

    value = float(match.group(1))
    unit = UNIT_ALIASES.get(match.group(2).lower())
    if unit is None:
        return name, None, None

    remainder = name[: match.start()] + name[match.end():]
    return remainder, value, unit


def make_match_key(name: str) -> str:
    """
    Reduce a menu name to the form used for comparison.

        "The CLASSIC Cheeseburger!"  ->  "cheeseburger"
        "Caesar   Salad"             ->  "caesar salad"

    Punctuation goes first, then filler words, then whitespace is collapsed.
    Removing filler before collapsing whitespace matters, since dropping a
    word leaves a double space behind.
    """
    cleaned = name.lower()
    # Keep letters, digits and spaces. Ampersand becomes "and" first so that
    # "Mac & Cheese" and "Mac and Cheese" agree.
    cleaned = cleaned.replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)

    words = [w for w in cleaned.split() if w and w not in FILLER_WORDS]

    # Every word being filler ("The House Special") would leave nothing to
    # match on, so in that case the cleaned text is kept as-is.
    if not words:
        words = cleaned.split()

    return " ".join(words)


def similarity(left: str, right: str) -> float:
    """
    How alike two match keys are, from 0 to 1.

    SequenceMatcher compares the longest runs of shared characters, so it
    tolerates a small edit, a plural, or a dropped word, while still scoring
    genuinely different dishes low.
    """
    return SequenceMatcher(None, left, right).ratio()


def find_match(
    candidate_key: str,
    existing_keys: list[str],
    threshold: float | None = None,
) -> tuple[str | None, float]:
    """
    Find the existing key that best matches a new one.

    Returns (best key or None, its score). None means nothing cleared the
    threshold and the caller should create a new item.

    An exact match short-circuits, which is both faster and removes any doubt
    in the common case.
    """
    if threshold is None:
        threshold = float(load_settings()["prices"]["match_threshold"])

    if candidate_key in existing_keys:
        return candidate_key, 1.0

    best_key: str | None = None
    best_score = 0.0
    for key in existing_keys:
        score = similarity(candidate_key, key)
        if score > best_score:
            best_key, best_score = key, score

    if best_score >= threshold:
        return best_key, best_score
    return None, best_score


# Commas are stripped from the text BEFORE this runs, so the pattern matches a
# plain run of digits with an optional decimal part.
#
# An earlier version of this pattern still described the comma grouping,
# written as \d{1,3}(?:,\d{3})*. With the commas already gone, "1299.00" made
# that pattern match just the first three digits, and "$1,299.00" was read as
# $129. Keeping the stripping and the pattern in agreement is the fix.
PRICE_PATTERN = re.compile(r"\$?\s*(\d+(?:\.\d{1,2})?)")


def parse_price(text: str) -> int | None:
    """
    Read a price out of a scrap of text, returning whole CENTS.

        "$12.99"      ->  1299
        "12.99"       ->  1299
        "$8"          ->  800
        "Market Price" -> None

    Cents are returned because money held as a float drifts: 0.1 + 0.2 does
    not equal 0.3 in binary floating point, and an index sums thousands of
    prices. Integers stay exact.

    A value outside the configured range is rejected. Menu pages are full of
    numbers that look like prices, and without the range check a phone number
    or a "since 1959" would enter the data as a price.
    """
    if not text:
        return None

    match = PRICE_PATTERN.search(text.replace(",", ""))
    if not match:
        return None

    try:
        dollars = float(match.group(1))
    except ValueError:
        return None

    settings = load_settings()["prices"]
    if not (float(settings["min_dollars"]) <= dollars <= float(settings["max_dollars"])):
        return None

    # round() before int() because 12.99 * 100 evaluates to 1298.9999... in
    # binary floating point, and int() would truncate it to 1298.
    return int(round(dollars * 100))


def clean_item_name(name: str) -> str:
    """Tidy a name for display, leaving its wording intact."""
    cleaned = re.sub(r"\s+", " ", name).strip(" -–—·|\t\n")
    return cleaned[:120]
