"""
menu_index - tracks restaurant menu prices in Reno and turns them into a
price index that can be compared against the official CPI.

The package splits along the stages of the pipeline, so each file answers one
question and can be read on its own:

    config.py     What restaurants and settings are configured?
    database.py   How are observations stored, and how do they come back out?
    scraper.py    How do pages get fetched, politely and legally?
    parser.py     How does a page of HTML become a list of items and prices?
    normalize.py  Is this the same item we saw last month under a new name?
    bls.py        What does the official CPI say?
    indexer.py    How do observations become an index number?
    seed.py       Where does demo data come from before real data exists?

The pipeline runs in that order: scrape, parse, normalize, store, index.
"""

__version__ = "1.0.0"
