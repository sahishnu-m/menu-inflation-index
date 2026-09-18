# Menu Inflation Index

Measures what eating out costs in Reno and Sparks by scraping restaurant menu
prices monthly, building a price index from a fixed basket of dishes, and
setting that against the official BLS series for food away from home.

**[Live app](https://menu-inflation-index.streamlit.app)**

---

## Motivation

Everyone says restaurants have got more expensive. The national CPI puts a
number on that for the country as a whole, and the country as a whole is not
where anybody eats. Reno is one metro area inside a figure built from
thousands of price quotes nationwide, so its own experience can differ and the
national number would never show it.

This project builds the local number directly: collect the same dishes from the
same restaurants month after month, and watch what those specific prices do.

---

## Quick start

```bash
git clone https://github.com/sahishnu-m/menu-inflation-index.git
cd menu-inflation-index

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt

python scripts/seed_demo_data.py    # simulated data, so the charts have shape
streamlit run app.py
```

It opens at `http://localhost:8501`. The BLS API needs no key, which is why
nothing here asks for one.

Other commands:

```bash
python scripts/check_robots.py            # which sites permit scraping
python scripts/run_scrape.py --dry-run    # parse live pages, store nothing
python scripts/run_scrape.py --only campo_reno --dry-run   # work on one site
python scripts/run_scrape.py              # the real monthly collection
python scripts/fetch_cpi.py               # refresh the CPI cache
python -m pytest tests/ -v                # 45 tests
```

---

## Method

### Why an average price would answer the wrong question

Averaging every price collected each month produces a number that moves
whenever the **mix** of items changes. If January holds 40 fast-food items and
10 steakhouse entrees, and February holds 40 and 30 because a steakhouse
published its full menu, the average leaps. Nothing got more expensive. The
basket changed.

Menus also gain expensive specials in December and lose them in January, and a
restaurant that stops publishing prices drops out of the average altogether.

### The fixed basket

A price index removes that by holding the basket still and letting only prices
move. This is a **Laspeyres-style index**: a basket chosen in one period is
priced again in later periods.

```
Index(t) = 100 x  sum( price(i, t) x quantity(i, 0) )
                 -----------------------------------
                  sum( price(i, 0) x quantity(i, 0) )
```

Quantities come from the base period and stay there, so every movement in the
number comes from prices. That is the family the CPI belongs to, which is what
makes the comparison meaningful.

This project has **no quantities** — nobody knows how many cheeseburgers Reno
buys — so every basket item gets a quantity of 1, and the formula reduces to
the sum of this month's basket prices over the sum of the same items' prices
last month. The consequences of that simplification are in *Limitations*.

### Matched items only

An item enters a comparison only when it appears in **both** months being
compared. Pricing an item that arrived halfway through would put something in
the numerator that the denominator lacks, and the ratio would measure menu
growth instead of inflation.

### Chaining

Holding one base period forever shrinks the basket as dishes disappear, until
the index rests on a handful of survivors. Instead each month is compared
against the one before it, where the overlap is largest, and those links are
multiplied into a series. This is close to how the real CPI handles items
entering and leaving the market.

### Handling renames, removals and resizes

Menus are written by people and the wording drifts. "Classic Cheeseburger"
becomes "Cheeseburger"; a typo gets fixed; whitespace changes.

Matching on exact text would treat each of those as a brand new item, restart
its price series, and drop the change between the old and new price out of the
index entirely. Since price change is the only thing this project measures,
that failure would empty out the result.

So the database separates two ideas. An **item** is a lasting thing whose
identity survives a rename. An **observation** records what a menu said on one
day, including the exact text printed at the time. New text is matched to
existing items with a cleaned "match key" and a similarity score, and the
original wording is never discarded, so a wrong match can be found and undone.

**Portion size is stored separately from the name.** A drink going from 16oz to
12oz at an unchanged price is a 33% increase per ounce that a price-only
comparison misses completely. Keeping size as its own field makes a resized
item a separate series, so a quiet shrink shows up rather than hiding.

### Outlier rejection

Month-over-month moves above 60% are recorded and excluded. A jump that large
is nearly always a parsing mistake or two different dishes wrongly merged, and
letting one through would swamp every genuine price move around it. The
dashboard lists everything it excluded.

---

## Results

The dashboard shows four things: the local index against the national CPI,
each price tier's own index, the individual dishes that moved most, and the
methodology with its limits.

Until real collection has run for a few months, it displays **simulated demo
data**, labelled as such wherever it appears. Real and simulated observations
are stored under separate labels in the same database and are never combined,
never averaged, and never drawn on one line. Every database read takes a
dataset argument, so the separation is enforced in the storage layer rather
than left to each caller to remember.

---

## Data collection and ethics

Prices come from public menu pages. The rules are enforced inside the fetch
path itself, so any future caller follows them:

1. **robots.txt is checked first for every site, and it binds.** No override
   flag exists.
2. **When robots.txt cannot be read, the fetch is refused.** Treating an
   unreadable robots.txt as permission gets the burden of proof backwards.
   Confirming permission is what grants it.
3. **One request every 3 seconds per host**, applied inside the fetch method so
   a later edit cannot skip it.
4. **The User-Agent names the project and links here**, so anyone seeing the
   traffic can identify it and ask for it to stop.
5. **A site's `Crawl-delay` is honoured** when it asks for a longer gap than
   the default.
6. **Nothing behind a login.** No credentials, no session handling.
7. **Blocks are obeyed.** Disguising the scraper as Chrome would very likely
   work, and a site blocking bots expresses the same preference as a robots.txt
   Disallow through a different mechanism. Honouring one while defeating the
   other would follow the letter of the rule and break its intent.

**What the first check found**, from `python scripts/check_robots.py`:

| Result | Count |
|---|---|
| Allowed by robots.txt | 16 of 20 |
| Refused, robots.txt unreadable | 4 of 20 |

The four refusals are McDonald's, Panda Express, Little Waldorf Saloon and
Inclined Burgers & Brews. Those sites sit behind protection that returns no
readable robots.txt to an identified bot, so the scraper declines to fetch
them. That is the fail-closed rule working as designed.

Playwright renders the pages that build their menus in JavaScript. It runs the
same robots check, the same rate limit and the same honest User-Agent. A real
browser engine makes JavaScript execute; it grants no permission that plain
HTTP lacked.

**What gets stored:** item names and prices. Raw menu HTML is cached to
`data/cache/` purely so a re-run avoids re-fetching, and `.gitignore` excludes
that folder. Whole copies of someone else's pages have no place in this
repository.

---

## Limitations

Worth reading before quoting any number from this.

**No quantity weights.** A real CPI weights each item by how much people buy,
from national expenditure surveys this project cannot access. Here every item
counts equally, and because the index compares sums of prices, expensive dishes
dominate anyway: a $42 steak moving 5% outweighs a $3 drink moving 40%.

**Small sample.** Twenty restaurants in one metro area, against the thousands
of quotes behind the official CPI. One restaurant reprinting its menu is
visible in the index. A gap against the CPI is a question worth asking rather
than a finding.

**Survivorship bias.** The basket can only hold dishes that stay on the menu. A
restaurant dropping an item it no longer wants to sell at that price removes
exactly the observation that would have shown the largest increase. That biases
the index downward by an amount nobody can measure from inside the data.

**Menu prices are list prices.** They exclude discounts, app-only deals, happy
hour, combo pricing and delivery markups. What people actually pay moves
differently, often by more.

**Four sites are unreachable**, so the sample skews toward restaurants that
permit scraping. Whether that group's prices behave like the ones that block it
is unknown.

**Selectors are unverified.** Every entry in `config/restaurants.yaml` is
currently `verified: false`, meaning nobody has checked its CSS selectors
against the live page. A selector matching nothing looks exactly like a
restaurant that removed its menu, so the scraper reports the difference and the
dashboard shows which are still unchecked.

**The CPI comparison is context rather than a grade.** `CUUR0000SEFV` is a US
city average, so Reno is a rounding error inside it. Both series are
unadjusted, which keeps the comparison fair.

---

## Adding a restaurant

Everything lives in `config/restaurants.yaml`; no Python changes are needed.

```yaml
  - id: my_restaurant
    name: "My Restaurant"
    category: casual          # fast_food | casual | mid_range
    url: "https://example.com/menu/"
    renderer: static          # static | javascript
    verified: false
    selectors:
      item: ".menu-item"      # the repeating block wrapping ONE item
      name: ".item-name"      # searched INSIDE that block
      price: ".item-price"    # searched INSIDE that block
```

Finding the selectors takes about five minutes:

1. Open the menu page and right-click an item name, then Inspect.
2. Find the repeating block that wraps one item, with its name and price inside.
3. Put that block's selector in `item`, and the two inner ones in `name` and
   `price`.
4. Run `python scripts/run_scrape.py --only my_restaurant --dry-run` to see
   what comes out without writing anything.
5. Set `verified: true` once it looks right.

Scoping `name` and `price` to within each `item` block is what keeps a name
attached to its own price. Selecting all names and all prices separately and
pairing them up would misalign the moment one item lacks a price, and every
later item would silently carry its neighbour's price.

---

## Running it monthly

`.github/workflows/monthly-scrape.yml` collects prices at 09:00 UTC on the 2nd
of each month and commits the updated database back to the repository, which is
what the deployed dashboard reads.

GitHub Actions is used rather than cron because cron needs a machine that is
switched on at the scheduled moment. A laptop that happens to be closed on the
2nd silently skips that month, and a hole in a price index cannot be repaired
later, since last month's menu prices are gone.

The 2nd rather than the 1st because restaurants often update their websites
during the 1st, and collecting a day later avoids catching some menus before
the change and some after.

Test it without waiting: **Actions** tab, **Monthly menu price collection**,
**Run workflow**.

---

## Project layout

```
menu-inflation-index/
├── app.py                          Streamlit dashboard
├── config/
│   ├── restaurants.yaml            the restaurants - edit this
│   └── settings.yaml               tuning numbers - edit this
├── menu_index/
│   ├── config.py                   loads and validates the YAML
│   ├── database.py                 SQLite schema and queries
│   ├── scraper.py                  polite fetching, static and browser
│   ├── parser.py                   HTML into items and prices
│   ├── normalize.py                renames, resizes, price parsing
│   ├── indexer.py                  the Laspeyres index
│   ├── bls.py                      the CPI comparison series
│   ├── pipeline.py                 one full collection run
│   └── seed.py                     simulated demo data
├── scripts/
│   ├── seed_demo_data.py
│   ├── run_scrape.py
│   ├── check_robots.py
│   └── fetch_cpi.py
├── .github/workflows/monthly-scrape.yml
└── tests/test_pipeline.py          45 tests
```

Each module answers one question, so any single file reads top to bottom
without the rest of the project in your head.

---

## Two bugs worth knowing about

Both were found by the tests, and both are commented where they live.

**A regex that disagreed with its own input.** `parse_price` stripped thousands
separators from the text and then matched with a pattern that still described
comma grouping, `\d{1,3}(?:,\d{3})*`. With the commas already gone, "1299.00"
matched only its first three digits, so `$1,299.00` was read as `$129`. Keeping
the stripping and the pattern in agreement is the fix.

**A filter that ran before the case it excluded.** `build_index` dropped items
appearing in fewer than two periods, then checked whether enough periods
existed. With a single month of data every item was filtered out first, so the
function returned an empty series where the honest answer is "here is the
starting point, with nothing yet to compare it against". Settling the
single-period case before filtering fixed it.

---

## License

MIT. CPI data from the U.S. Bureau of Labor Statistics, public domain. Menu
prices are facts about publicly posted prices; restaurant names and trademarks
belong to their owners.
