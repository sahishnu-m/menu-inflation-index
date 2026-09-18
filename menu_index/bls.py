"""
bls.py - fetches the official CPI series this project compares itself against.

THE SERIES
CUUR0000SEFV is "Food away from home", US city average, all urban consumers,
not seasonally adjusted. In plain terms it is the government's measure of what
eating out costs, which is the national counterpart to the local number this
project builds.

Two details of that series matter when reading a chart of it:

  * US CITY AVERAGE. It covers the whole country, so Reno is a rounding error
    inside it. A gap between this project's index and the CPI can be a real
    local difference, and can equally be the small sample here. The honest
    reading is that the CPI is context and not a grade.

  * NOT SEASONALLY ADJUSTED. The raw series still contains the regular
    calendar pattern of the year. That is the right choice here, because this
    project's own index is unadjusted too, and comparing an adjusted series
    with an unadjusted one would show differences that are purely an artifact
    of the adjustment.

THE API
The public v2 endpoint answers without a registration key, subject to a daily
request cap. A key raises that cap and is read from the BLS_API_KEY
environment variable when present. No key is stored in this repository,
because a public repository keeps anything committed to it forever, including
after a later commit removes it.

Responses are cached on disk for a day. The series updates monthly, so a
normal day makes at most one call.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time

import pandas as pd
import requests

from .config import DATA_DIR, load_settings, user_agent

logger = logging.getLogger(__name__)

CACHE_PATH = DATA_DIR / "bls_cpi_cache.json"

# BLS period codes: M01 to M12 are months. M13 is an annual average, which has
# no place in a monthly series and gets filtered out.
MONTH_PERIODS = {f"M{month:02d}" for month in range(1, 13)}


class BLSUnavailable(RuntimeError):
    """Raised when the CPI series cannot be retrieved."""


def _cache_is_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    hours = float(load_settings()["bls"]["cache_hours"])
    age = (time.time() - CACHE_PATH.stat().st_mtime) / 3600.0
    return age < hours


def fetch_cpi(
    start_year: int | None = None,
    end_year: int | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch the CPI series as a DataFrame of period and value.

    Returns columns:
        period   'YYYY-MM', matching this project's own period format
        cpi      the index value as published
        year, month

    Defaults to the last six calendar years, which comfortably covers any
    history this project will have collected while staying inside the range
    the keyless endpoint allows.
    """
    settings = load_settings()["bls"]

    today = dt.date.today()
    end_year = end_year or today.year
    start_year = start_year or (end_year - 5)

    if use_cache and _cache_is_fresh():
        logger.info("Using cached CPI data")
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return _to_frame(payload, settings["series_id"])

    body = {
        "seriesid": [settings["series_id"]],
        "startyear": str(start_year),
        "endyear": str(end_year),
    }

    # The key is added only when the environment supplies one, so the request
    # works either way.
    api_key = os.environ.get("BLS_API_KEY")
    if api_key:
        body["registrationkey"] = api_key
        logger.info("Using the BLS API key from the environment")

    try:
        response = requests.post(
            settings["api_url"],
            json=body,
            headers={"Content-Type": "application/json", "User-Agent": user_agent()},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as exc:
        # Fall back to a stale cache rather than showing nothing. An old CPI
        # reading is still informative, and the dashboard labels its age.
        if CACHE_PATH.exists():
            logger.warning("BLS request failed (%s); using the stale cache", exc)
            payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            return _to_frame(payload, settings["series_id"])
        raise BLSUnavailable(f"Could not reach the BLS API: {exc}") from exc

    if payload.get("status") != "REQUEST_SUCCEEDED":
        messages = "; ".join(payload.get("message", [])) or "no reason given"
        raise BLSUnavailable(f"BLS refused the request: {messages}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")

    return _to_frame(payload, settings["series_id"])


def _to_frame(payload: dict, series_id: str) -> pd.DataFrame:
    """Convert the BLS JSON into a tidy monthly frame."""
    series_list = payload.get("Results", {}).get("series", [])
    series = next((s for s in series_list if s.get("seriesID") == series_id), None)
    if series is None or not series.get("data"):
        raise BLSUnavailable(f"The response contained no data for {series_id}.")

    rows = []
    for entry in series["data"]:
        period_code = entry.get("period", "")
        if period_code not in MONTH_PERIODS:
            continue  # skips M13, the annual average
        try:
            value = float(entry["value"])
        except (KeyError, ValueError):
            continue
        year = int(entry["year"])
        month = int(period_code[1:])
        rows.append({
            "period": f"{year:04d}-{month:02d}",
            "cpi": value,
            "year": year,
            "month": month,
        })

    if not rows:
        raise BLSUnavailable(f"No monthly observations found for {series_id}.")

    # BLS returns newest first; charts want oldest first.
    return pd.DataFrame(rows).sort_values("period").reset_index(drop=True)


def cpi_change_pct(frame: pd.DataFrame, periods: list[str]) -> float | None:
    """
    Percentage change in the CPI across the same span as another series.

    Takes the list of periods from this project's index so the two figures
    cover identical months. Comparing a 12-month local change against a
    6-month national change would be meaningless, and this keeps the spans
    aligned by construction.
    """
    if frame.empty or len(periods) < 2:
        return None

    overlap = frame[frame["period"].isin(periods)].sort_values("period")
    if len(overlap) < 2:
        return None

    first = float(overlap.iloc[0]["cpi"])
    last = float(overlap.iloc[-1]["cpi"])
    return (last / first - 1.0) * 100.0 if first else None


def cache_age_hours() -> float | None:
    """How old the cached CPI response is, for the dashboard footer."""
    if not CACHE_PATH.exists():
        return None
    return (time.time() - CACHE_PATH.stat().st_mtime) / 3600.0
