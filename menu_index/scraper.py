"""
scraper.py - fetches menu pages, politely and within the rules.

READ THIS BEFORE RUNNING ANYTHING HERE.

Scraping is the part of this project with real ethical weight, so the rules
live inside the fetch path itself. Anyone calling this code follows them
whether they know about them or add a new caller years from now.

  1. ROBOTS.TXT IS CHECKED FIRST, AND IT BINDS.
     Before any page is fetched, that site's robots.txt is read and asked
     about the specific URL. A refusal means the page is skipped and the
     reason recorded. There is no override flag. The fact that robots.txt is
     advisory in a legal sense leaves the request itself worth respecting.

  2. WHEN ROBOTS.TXT CANNOT BE READ, THE FETCH IS REFUSED.
     Most scrapers treat an unreadable robots.txt as permission, which gets
     the burden of proof backwards. Confirming permission is what grants it.
     Failing closed costs a page that might have been allowed; failing open
     risks hammering a site that asked to be left alone.

  3. ONE REQUEST EVERY THREE SECONDS PER HOST.
     Enforced by a sleep inside the fetch method. These are restaurant
     websites on small hosting plans, and a fast scraper is hard to tell
     apart from an attack.

  4. THE USER-AGENT SAYS WHAT THIS IS.
     It names the project and links to the repository, so anyone seeing the
     traffic can identify it and ask for it to stop.

  5. THE BROWSER PATH FOLLOWS THE SAME RULES.
     Playwright drives a real browser for pages that build their menu in
     JavaScript. It reuses the same robots check, the same rate limit and the
     same User-Agent. A real browser engine makes JavaScript run; it grants no
     permission that plain HTTP lacked.

  6. NOTHING BEHIND A LOGIN, AND NO EVADING A BLOCK.
     There are no credentials here and no session handling. Where a site
     blocks this scraper, that block is recorded and obeyed. Disguising the
     User-Agent as Chrome would very likely work, and it would break the
     intent of the rule while keeping its letter.

WHAT THIS MEANS IN PRACTICE
Several large chains block automated access outright. Their pages are skipped
and the reason is logged, which is a result rather than a failure. The project
reports what it could collect and stays honest about the rest.

CACHING
Pages are written to data/cache/ keyed by a hash of the URL. A re-run inside
the cache window reads from disk and sends no request. That keeps development
iterations free and keeps repeated load off the restaurants entirely. The
cache folder is excluded by .gitignore, so raw pages never enter the
repository.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import DATA_DIR, Restaurant, load_settings, user_agent

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "cache"


@dataclass
class FetchResult:
    """The outcome of trying to fetch one restaurant's menu page."""

    restaurant_id: str
    status: str  # "fetched" | "skipped" | "error"
    reason: str
    html: str | None = None
    from_cache: bool = False
    fetched_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "fetched" and bool(self.html)


class PoliteFetcher:
    """
    A fetcher that cannot be used impolitely.

    DESIGN CHOICE: the rate limit and the robots check live inside `fetch`
    rather than being the caller's responsibility. Left to callers, some
    future path through the code forgets one, which is how most
    badly-behaved scrapers come about. Making the careful path the only
    available path is more reliable than remembering to be careful.
    """

    def __init__(self, rate_limit_seconds: float | None = None) -> None:
        settings = load_settings()["scraping"]
        self.rate_limit = (
            rate_limit_seconds
            if rate_limit_seconds is not None
            else float(settings["rate_limit_seconds"])
        )
        self.timeout = int(settings["request_timeout_seconds"])
        self.cache_hours = float(settings["cache_hours"])
        self.fail_closed = bool(settings["fail_closed_on_unreadable_robots"])
        self.settle_seconds = float(settings["javascript_settle_seconds"])

        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent()})

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # -- rate limiting ------------------------------------------------------

    def _host_of(self, url: str) -> str:
        parts = urllib.parse.urlparse(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _wait_turn(self, url: str) -> None:
        """
        Sleep long enough to honour the rate limit for this host.

        Tracked per host so that visiting twenty different restaurants stays
        brisk while any single site is still approached slowly. The limit
        exists to protect each server, and separate servers do not share load.
        """
        host = self._host_of(url)
        last = self._last_request_at.get(host, 0.0)
        remaining = self.rate_limit - (time.monotonic() - last)
        if remaining > 0:
            logger.debug("Rate limit: waiting %.1fs for %s", remaining, host)
            time.sleep(remaining)
        self._last_request_at[host] = time.monotonic()

    # -- robots.txt ---------------------------------------------------------

    def _robots_for(self, url: str):
        """Fetch and parse a host's robots.txt once per run, then reuse it."""
        host = self._host_of(url)
        if host in self._robots:
            return self._robots[host]

        robots_url = f"{host}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)

        self._wait_turn(url)  # robots.txt is a request too and counts
        try:
            response = self._session.get(robots_url, timeout=self.timeout)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
                logger.info("Read robots.txt from %s", robots_url)
                self._robots[host] = parser
            elif response.status_code in (401, 403):
                # A protected robots.txt is a site saying it wants no robots.
                logger.warning("robots.txt at %s is protected; treating host as closed", robots_url)
                self._robots[host] = None
            else:
                # By the standard, no robots.txt means nothing is disallowed.
                logger.info("No robots.txt at %s (HTTP %s)", robots_url, response.status_code)
                parser.parse([])
                self._robots[host] = parser
        except requests.exceptions.RequestException as exc:
            logger.warning("Could not read %s: %s", robots_url, exc)
            self._robots[host] = None

        return self._robots[host]

    def allowed(self, url: str) -> tuple[bool, str]:
        """May this URL be fetched? Returns (allowed, reason)."""
        parser = self._robots_for(url)

        if parser is None:
            if self.fail_closed:
                return False, "robots.txt could not be read; refusing (fail closed)"
            return True, "robots.txt unreadable and fail-closed is off"

        agent = user_agent()
        if parser.can_fetch(agent, url):
            delay = parser.crawl_delay(agent)
            if delay and float(delay) > self.rate_limit:
                # A site asking for a longer gap than our default gets it.
                # Honouring Crawl-delay is the point of publishing one.
                self.rate_limit = float(delay)
                return True, f"allowed; honouring site Crawl-delay of {delay}s"
            return True, "allowed by robots.txt"

        return False, "disallowed by robots.txt"

    # -- caching ------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        """
        Where a URL's cached copy lives.

        The filename is a hash of the URL because URLs contain characters that
        are illegal in filenames and can exceed the length a filesystem
        accepts. A hash is fixed-length and always safe.
        """
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        return CACHE_DIR / f"{digest}.html"

    def _read_cache(self, url: str) -> str | None:
        """Return a cached page when it is present and still young enough."""
        path = self._cache_path(url)
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600.0
        if age_hours > self.cache_hours:
            return None
        logger.info("Cache hit (%.1fh old) for %s", age_hours, url)
        return path.read_text(encoding="utf-8", errors="replace")

    def _write_cache(self, url: str, html: str) -> None:
        self._cache_path(url).write_text(html, encoding="utf-8", errors="replace")

    # -- fetching -----------------------------------------------------------

    def fetch(self, restaurant: Restaurant, use_cache: bool = True) -> FetchResult:
        """
        Fetch one restaurant's menu page, choosing the right method for it.

        Order of operations matters: the cache is consulted BEFORE robots.txt,
        because serving a page already on disk sends no request at all and so
        raises no question of permission. Every path that touches the network
        passes through the robots check first.
        """
        now = dt.datetime.now().isoformat(timespec="seconds")
        url = restaurant.url

        if use_cache:
            cached = self._read_cache(url)
            if cached is not None:
                return FetchResult(
                    restaurant.id, "fetched", "served from local cache",
                    html=cached, from_cache=True, fetched_at=now,
                )

        is_allowed, reason = self.allowed(url)
        if not is_allowed:
            logger.warning("SKIP %s: %s", restaurant.id, reason)
            return FetchResult(restaurant.id, "skipped", reason, fetched_at=now)

        if restaurant.needs_browser:
            result = self._fetch_with_browser(restaurant)
        else:
            result = self._fetch_static(restaurant)

        if result.ok and result.html:
            self._write_cache(url, result.html)
        return result

    def _fetch_static(self, restaurant: Restaurant) -> FetchResult:
        """Plain HTTP, for pages whose menu is in the delivered HTML."""
        now = dt.datetime.now().isoformat(timespec="seconds")
        self._wait_turn(restaurant.url)
        logger.info("Fetching %s", restaurant.url)

        try:
            response = self._session.get(restaurant.url, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            return FetchResult(restaurant.id, "error", f"request failed: {exc}", fetched_at=now)

        if response.status_code != 200:
            # A 403 or 404 on a URL that works in a browser usually means the
            # site refuses automated clients, through a bot filter or by
            # serving different content by User-Agent. That is recorded and
            # respected. Pretending to be Chrome would likely retrieve the
            # page and would defeat a control the site chose to put there.
            hint = ""
            if response.status_code in (401, 403, 406, 429):
                hint = (" - the site appears to refuse automated clients; "
                        "read it in a browser instead of disguising the scraper")
            return FetchResult(
                restaurant.id, "error",
                f"HTTP {response.status_code}{hint}", fetched_at=now,
            )

        return FetchResult(
            restaurant.id, "fetched", "fetched over HTTP",
            html=response.text, fetched_at=now,
        )

    def _fetch_with_browser(self, restaurant: Restaurant) -> FetchResult:
        """
        Render with Playwright, for menus assembled in the browser.

        Plain HTTP on these pages returns an almost empty shell plus a bundle
        of JavaScript, so the prices simply are not in the response. Running a
        real browser engine executes that JavaScript and produces the finished
        page.

        Playwright is an optional dependency. Importing it here rather than at
        the top of the file means someone who only tracks static sites can
        install a much smaller set of packages, and gets a clear instruction
        instead of an import error if they need it later.
        """
        now = dt.datetime.now().isoformat(timespec="seconds")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(
                restaurant.id, "skipped",
                "needs a browser; install with: pip install playwright "
                "&& python -m playwright install chromium",
                fetched_at=now,
            )

        self._wait_turn(restaurant.url)
        logger.info("Rendering %s with a browser", restaurant.url)

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                # The same honest User-Agent as the HTTP path. The browser
                # changes what can be rendered and changes nothing about who
                # this is.
                context = browser.new_context(user_agent=user_agent())
                page = context.new_page()
                page.goto(restaurant.url, wait_until="networkidle",
                          timeout=self.timeout * 1000)
                # Menus often arrive a moment after the network goes quiet.
                page.wait_for_timeout(int(self.settle_seconds * 1000))
                html = page.content()
                browser.close()
        except Exception as exc:  # Playwright raises several unrelated types
            return FetchResult(restaurant.id, "error", f"browser render failed: {exc}", fetched_at=now)

        return FetchResult(
            restaurant.id, "fetched", "rendered in a browser",
            html=html, fetched_at=now,
        )


def cache_summary() -> dict[str, object]:
    """How much is cached on disk, for the dashboard and the CLI."""
    if not CACHE_DIR.exists():
        return {"files": 0, "megabytes": 0.0}
    files = list(CACHE_DIR.glob("*.html"))
    total = sum(f.stat().st_size for f in files)
    return {"files": len(files), "megabytes": round(total / 1_048_576, 2)}
