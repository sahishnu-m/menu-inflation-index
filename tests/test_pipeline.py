"""
Tests for price parsing, item matching, and the index calculation.

WHY THESE TESTS EXIST
The index is a number nobody can check by eye. A wrong price index looks
exactly like a right one, which makes this the kind of code where a quiet
mistake survives for months. The tests below pin the BEHAVIOUR that has to
hold, while leaving the tuning numbers free to change:

  * a fixed basket of flat prices produces a flat index
  * a uniform 10% rise produces an index of 110
  * items appearing partway through leave the index alone
  * a renamed item keeps its series
  * simulated data stays out of real results

Each test builds its own data, so nothing touches the network or the real
database.

Run them with:  python -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from menu_index import database as db  # noqa: E402
from menu_index.config import load_restaurants, load_settings  # noqa: E402
from menu_index.indexer import (  # noqa: E402
    build_index,
    item_price_changes,
    rebase,
)
from menu_index.normalize import (  # noqa: E402
    extract_size,
    find_match,
    make_match_key,
    parse_price,
    similarity,
)


def observations(rows: list[tuple[str, int, float]]) -> pd.DataFrame:
    """Build an observations frame from (period, item_id, price) tuples."""
    return pd.DataFrame(
        [
            {
                "period": period,
                "item_id": item_id,
                "price": price,
                "canonical_name": f"item {item_id}",
                "restaurant_id": "r1",
                "restaurant_name": "Test Restaurant",
                "category": "casual",
            }
            for period, item_id, price in rows
        ]
    )


class TestPriceParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("$12.99", 1299),
            ("12.99", 1299),
            ("$8", 800),
            ("  $ 15.50  ", 1550),
            ("$249.99", 24999),
        ],
    )
    def test_reads_common_formats(self, text, expected):
        assert parse_price(text) == expected

    def test_thousands_separators_are_stripped_before_matching(self):
        """
        Commas are removed before the pattern runs, so a grouped number reads
        as its full value. $1,299 then falls above the menu ceiling and is
        rejected, which is the correct outcome: a four-figure figure on a menu
        page is a gift-card total or a phone number rather than a dish.

        This matters because an earlier version stripped the commas while the
        pattern still described them, and "$1,299.00" was read as $129.
        """
        assert parse_price("$1,299.00") is None
        assert parse_price("$1,299.00") != 12900

    def test_returns_cents_as_an_exact_integer(self):
        """
        The reason money is stored in cents. 12.99 * 100 evaluates to
        1298.9999... in binary floating point, so a naive int() gives 1298.
        """
        assert parse_price("$12.99") == 1299
        assert parse_price("$0.99") == 99

    @pytest.mark.parametrize("text", ["Market Price", "", "Call for pricing", "MP"])
    def test_rejects_text_without_a_price(self, text):
        assert parse_price(text) is None

    def test_rejects_numbers_outside_the_plausible_range(self):
        """A year or a phone number on a menu page should never become a price."""
        assert parse_price("2026") is None      # above max_dollars
        assert parse_price("$0.01") is None     # below min_dollars

    def test_rejects_a_price_above_the_configured_ceiling(self):
        ceiling = float(load_settings()["prices"]["max_dollars"])
        assert parse_price(f"${ceiling + 100:.2f}") is None


class TestSizeExtraction:
    @pytest.mark.parametrize(
        "name,value,unit",
        [
            ("Soft Drink (16 oz)", 16.0, "oz"),
            ("Chicken Wings (12 pc)", 12.0, "pc"),
            ("6-Piece Nuggets", 6.0, "pc"),
            ("Ribeye Steak (12 oz)", 12.0, "oz"),
            ("Cheeseburger", None, None),
        ],
    )
    def test_reads_sizes(self, name, value, unit):
        _, got_value, got_unit = extract_size(name)
        assert got_value == value
        assert got_unit == unit

    def test_unit_spellings_agree(self):
        """"ounce", "ounces" and "oz" describe the same unit."""
        assert extract_size("Drink 16 ounces")[2] == "oz"
        assert extract_size("Drink 16 oz")[2] == "oz"


class TestMatching:
    def test_filler_words_are_ignored(self):
        assert make_match_key("The Classic Cheeseburger") == make_match_key("Cheeseburger")

    def test_case_and_punctuation_are_ignored(self):
        assert make_match_key("BBQ Burger!") == make_match_key("bbq burger")

    def test_ampersand_and_and_agree(self):
        assert make_match_key("Mac & Cheese") == make_match_key("Mac and Cheese")

    def test_a_rename_still_matches(self):
        """The case this whole module exists for."""
        old = make_match_key("Classic Cheeseburger")
        new = make_match_key("Cheeseburger")
        matched, score = find_match(new, [old])
        assert matched == old
        assert score >= 0.88

    def test_different_dishes_stay_apart(self):
        salad = make_match_key("Caesar Salad")
        steak = make_match_key("Ribeye Steak")
        matched, _ = find_match(steak, [salad])
        assert matched is None

    def test_a_double_is_not_the_single(self):
        """
        The dangerous false merge. Treating these as one item would invent a
        large price jump from nothing.
        """
        single = make_match_key("Cheeseburger")
        double = make_match_key("Double Cheeseburger")
        assert similarity(single, double) < 0.88


class TestIndex:
    def test_flat_prices_give_a_flat_index(self):
        frame = observations([
            ("2026-01", 1, 10.0), ("2026-01", 2, 20.0),
            ("2026-02", 1, 10.0), ("2026-02", 2, 20.0),
            ("2026-03", 1, 10.0), ("2026-03", 2, 20.0),
        ])
        result = build_index(frame)
        assert [round(p.value, 2) for p in result.points] == [100.0, 100.0, 100.0]

    def test_a_uniform_ten_percent_rise_gives_110(self):
        frame = observations([
            ("2026-01", 1, 10.0), ("2026-01", 2, 20.0),
            ("2026-02", 1, 11.0), ("2026-02", 2, 22.0),
        ])
        result = build_index(frame)
        assert round(result.points[-1].value, 2) == 110.0

    def test_compounding_chains_correctly(self):
        """Two consecutive 10% rises compound to 121, rather than adding to 120."""
        frame = observations([
            ("2026-01", 1, 10.0),
            ("2026-02", 1, 11.0),
            ("2026-03", 1, 12.1),
        ])
        result = build_index(frame)
        assert round(result.points[-1].value, 1) == 121.0

    def test_a_new_item_does_not_move_the_index(self):
        """
        The reason for the matched-item rule. Item 2 arrives in February at a
        high price. Prices of existing items are unchanged, so the index holds
        at 100 and the new arrival changes nothing.
        """
        frame = observations([
            ("2026-01", 1, 10.0),
            ("2026-02", 1, 10.0), ("2026-02", 2, 90.0),
            ("2026-03", 1, 10.0), ("2026-03", 2, 90.0),
        ])
        result = build_index(frame)
        assert round(result.points[1].value, 2) == 100.0

    def test_a_disappearing_item_does_not_move_the_index(self):
        frame = observations([
            ("2026-01", 1, 10.0), ("2026-01", 2, 50.0),
            ("2026-02", 1, 10.0), ("2026-02", 2, 50.0),
            ("2026-03", 1, 10.0),
        ])
        result = build_index(frame)
        assert round(result.points[-1].value, 2) == 100.0

    def test_an_implausible_jump_is_excluded(self):
        """A parse error producing a 10x price should be kept out of the index."""
        frame = observations([
            ("2026-01", 1, 10.0), ("2026-01", 2, 20.0),
            ("2026-02", 1, 10.0), ("2026-02", 2, 200.0),
        ])
        result = build_index(frame)
        assert result.excluded, "the 900% move should have been flagged"
        assert round(result.points[-1].value, 2) == 100.0

    def test_expensive_items_dominate_without_quantity_weights(self):
        """
        Documents a known weakness rather than a bug. With equal weights, a
        $100 item moving 10% outweighs a $2 item moving 50%. The README states
        this; the test makes sure the behaviour stays as described.
        """
        frame = observations([
            ("2026-01", 1, 2.0), ("2026-01", 2, 100.0),
            ("2026-02", 1, 3.0), ("2026-02", 2, 110.0),
        ])
        result = build_index(frame)
        # (3 + 110) / (2 + 100) = 1.1078...
        assert 110 < result.points[-1].value < 111

    def test_a_single_period_yields_only_the_base(self):
        result = build_index(observations([("2026-01", 1, 10.0)]))
        assert len(result.points) == 1
        assert result.points[0].value == 100.0

    def test_empty_input_is_handled(self):
        result = build_index(pd.DataFrame(columns=["period", "item_id", "price"]))
        assert result.points == []
        assert result.to_frame().empty


class TestRebase:
    def test_two_series_meet_at_100_in_the_base_period(self):
        """
        What makes the menu index and the CPI comparable on one chart. The CPI
        sits near 390 while the index starts at 100, so plotting them raw
        flattens one against the axis.
        """
        cpi = pd.DataFrame({
            "period": ["2026-01", "2026-02", "2026-03"],
            "cpi": [380.0, 384.0, 390.0],
        })
        rebased = rebase(cpi, "cpi", "2026-01")
        assert round(float(rebased.iloc[0]["cpi"]), 2) == 100.0
        # 390/380 is a rise of about 2.6%
        assert round(float(rebased.iloc[2]["cpi"]), 1) == 102.6

    def test_a_missing_base_period_leaves_the_frame_alone(self):
        frame = pd.DataFrame({"period": ["2026-01"], "cpi": [380.0]})
        assert rebase(frame, "cpi", "2025-01").iloc[0]["cpi"] == 380.0


class TestItemChanges:
    def test_increases_and_decreases_are_separated(self):
        frame = observations([
            ("2026-01", 1, 10.0), ("2026-01", 2, 20.0),
            ("2026-02", 1, 12.0), ("2026-02", 2, 18.0),
        ])
        increases, decreases = item_price_changes(frame)
        assert len(increases) == 1 and increases.iloc[0]["change_pct"] == 20.0
        assert len(decreases) == 1 and decreases.iloc[0]["change_pct"] == -10.0

    def test_an_item_seen_once_is_skipped(self):
        increases, decreases = item_price_changes(observations([("2026-01", 1, 10.0)]))
        assert increases.empty and decreases.empty


class TestDatasetSeparation:
    def test_reads_are_confined_to_one_dataset(self, tmp_path):
        """
        The guarantee that keeps invented demo numbers out of real results.
        Both datasets are written, then each read returns only its own.
        """
        path = tmp_path / "test.sqlite"

        with db.connect(path) as connection:
            db.sync_restaurants(connection)
            restaurant = load_restaurants()[0]
            item_id = db.upsert_item(
                connection, restaurant.id, "Test Burger", "test burger",
                None, None, "2026-01",
            )
            db.record_observation(connection, item_id, "2026-01", 1000, "Test Burger", db.REAL)
            db.record_observation(connection, item_id, "2026-01", 9999, "Test Burger", db.SIMULATED)

        real = db.load_observations(db.REAL, path)
        simulated = db.load_observations(db.SIMULATED, path)

        assert len(real) == 1 and real.iloc[0]["price"] == 10.00
        assert len(simulated) == 1 and simulated.iloc[0]["price"] == 99.99

    def test_clearing_one_dataset_leaves_the_other(self, tmp_path):
        path = tmp_path / "test.sqlite"

        with db.connect(path) as connection:
            db.sync_restaurants(connection)
            restaurant = load_restaurants()[0]
            item_id = db.upsert_item(
                connection, restaurant.id, "Test Burger", "test burger",
                None, None, "2026-01",
            )
            db.record_observation(connection, item_id, "2026-01", 1000, "Test Burger", db.REAL)
            db.record_observation(connection, item_id, "2026-01", 9999, "Test Burger", db.SIMULATED)

        db.clear_dataset(db.SIMULATED, path)

        assert len(db.load_observations(db.REAL, path)) == 1
        assert len(db.load_observations(db.SIMULATED, path)) == 0

    def test_a_rerun_in_one_month_replaces_rather_than_duplicates(self, tmp_path):
        path = tmp_path / "test.sqlite"

        with db.connect(path) as connection:
            db.sync_restaurants(connection)
            restaurant = load_restaurants()[0]
            item_id = db.upsert_item(
                connection, restaurant.id, "Burger", "burger", None, None, "2026-01",
            )
            db.record_observation(connection, item_id, "2026-01", 1000, "Burger", db.REAL)
            db.record_observation(connection, item_id, "2026-01", 1200, "Burger", db.REAL)

        frame = db.load_observations(db.REAL, path)
        assert len(frame) == 1, "a second reading in one month should overwrite"
        assert frame.iloc[0]["price"] == 12.00


class TestConfig:
    def test_restaurants_yaml_parses(self):
        restaurants = load_restaurants()
        assert 15 <= len(restaurants) <= 25, "the brief asked for 15 to 25 restaurants"

    def test_every_restaurant_is_complete(self):
        for restaurant in load_restaurants():
            assert restaurant.url.startswith("https://"), f"{restaurant.id} needs https"
            assert restaurant.selectors.item
            assert restaurant.selectors.name
            assert restaurant.selectors.price

    def test_all_three_categories_are_represented(self):
        categories = {r.category for r in load_restaurants()}
        assert categories == {"fast_food", "casual", "mid_range"}

    def test_each_category_holds_enough_restaurants(self):
        """A category index built on one restaurant describes that restaurant."""
        from collections import Counter

        counts = Counter(r.category for r in load_restaurants())
        for category, count in counts.items():
            assert count >= 3, f"{category} has only {count} restaurants"
