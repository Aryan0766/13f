"""Sector labels and the two sector views (market screener, and your own filers).

13F filings carry no sector, so each ticker gets one from the SEC's own industry (SIC)
code, grouped into about 11 broad sectors. SIC is coarse: a few companies land in an odd
bucket (for example, internet platforms are classed as Technology), and anything without
a ticker or a code is "Unclassified".

The sector views show direction only (being added to / trimmed). No dollar amounts.

Build the labels once (about 10 minutes), then they are cached on disk:
    python -m thirteenf_processor.sector --build
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request

import pandas as pd

from .processor import PROJECT_ROOT, configure_environment

log = logging.getLogger("thirteenf")

MAP_FILE = PROJECT_ROOT / "data" / "index" / "sectors.json"
UNCLASSIFIED = "Unclassified"
MIN_STOCKS = 5  # a sector with fewer stocks / moves than this gets no verdict
FLAT_PCT = 1.0  # a holder count that moved less than this % counts as flat
LEAN = 0.2  # filer view: net moves must be this share of all moves to call a direction

# (low SIC, high SIC, sector). First match wins, so specific ranges come first.
SIC_RULES: list[tuple[int, int, str]] = [
    (6770, 6770, UNCLASSIFIED),  # blank-check shells (SPACs)
    (6798, 6798, "Real Estate"),  # REITs
    (6500, 6599, "Real Estate"),
    (6000, 6799, "Financials"),
    (1300, 1399, "Energy"),
    (2900, 2999, "Energy"),
    (4600, 4699, "Energy"),  # pipelines
    (5170, 5179, "Energy"),
    (4950, 4959, "Industrials"),  # waste / sanitary services
    (4900, 4999, "Utilities"),
    (4800, 4899, "Communication Services"),
    (2710, 2799, "Communication Services"),  # publishing
    (7310, 7319, "Communication Services"),  # advertising
    (7810, 7829, "Communication Services"),  # film and TV
    (3570, 3579, "Technology"),
    (3661, 3679, "Technology"),
    (7370, 7379, "Technology"),
    (2830, 2836, "Health Care"),
    (3840, 3851, "Health Care"),
    (8000, 8099, "Health Care"),
    (5120, 5129, "Health Care"),
    (5910, 5912, "Consumer Staples"),  # drug stores
    (100, 999, "Consumer Staples"),  # agriculture
    (2000, 2199, "Consumer Staples"),  # food, drink, tobacco
    (2840, 2844, "Consumer Staples"),  # household / personal products
    (5140, 5149, "Consumer Staples"),
    (5400, 5499, "Consumer Staples"),  # food stores
    (2200, 2399, "Consumer Discretionary"),  # textiles, apparel
    (2500, 2599, "Consumer Discretionary"),  # furniture
    (3100, 3149, "Consumer Discretionary"),  # leather, footwear
    (3630, 3639, "Consumer Discretionary"),  # household appliances
    (3710, 3719, "Consumer Discretionary"),  # autos
    (3750, 3751, "Consumer Discretionary"),
    (3940, 3949, "Consumer Discretionary"),  # toys, sporting goods
    (5200, 5399, "Consumer Discretionary"),  # retail
    (5500, 5999, "Consumer Discretionary"),  # retail, restaurants
    (7000, 7099, "Consumer Discretionary"),  # hotels
    (7900, 7999, "Consumer Discretionary"),  # recreation
    (1000, 1299, "Materials"),  # mining
    (1400, 1499, "Materials"),
    (2400, 2499, "Materials"),  # lumber
    (2600, 2699, "Materials"),  # paper
    (2800, 2829, "Materials"),  # chemicals
    (2850, 2899, "Materials"),
    (3080, 3089, "Materials"),  # plastics
    (3200, 3299, "Materials"),  # glass, cement
    (3300, 3399, "Materials"),  # metals
    (1500, 1799, "Industrials"),  # construction
    (3400, 3599, "Industrials"),  # machinery, equipment
    (3600, 3659, "Industrials"),  # electrical equipment
    (3680, 3699, "Industrials"),
    (3700, 3799, "Industrials"),  # aerospace, transport equipment
    (3800, 3839, "Industrials"),  # instruments
    (4000, 4799, "Industrials"),  # transportation
    (5000, 5099, "Industrials"),  # wholesale, durable goods
    (7380, 7399, "Industrials"),  # business services
]


def sector_for_sic(sic) -> str:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return UNCLASSIFIED
    for low, high, sector in SIC_RULES:
        if low <= code <= high:
            return sector
    return UNCLASSIFIED


# --------------------------------------------------------------------------- the cached map


def load_map() -> dict:
    """{ticker: {"sic": code or None, "sector": name}}; empty if never built."""
    try:
        return json.loads(MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_map(data: dict) -> None:
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(data, indent=0, sort_keys=True), encoding="utf-8")


def add_sector(df: pd.DataFrame, ticker_col: str = "Ticker") -> pd.DataFrame:
    labels = {k: v["sector"] for k, v in load_map().items()}
    out = df.copy()
    out["Sector"] = (
        out[ticker_col]
        .fillna("")
        .astype(str)
        .str.upper()
        .map(labels)
        .fillna(UNCLASSIFIED)
    )
    return out


# --------------------------------------------------------------------------- building it


def _get_json(url: str, agent: str):
    req = urllib.request.Request(
        url, headers={"User-Agent": agent, "Accept-Encoding": "identity"}
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            time.sleep(2 * (attempt + 1))  # 429 / 5xx: wait and retry
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(2 * (attempt + 1))
    return None


def _wanted_tickers() -> set[str]:
    """Every ticker the two views might show."""
    from . import bulk, combine

    found: set[str] = set()
    for early in (False, True):
        try:
            found |= set(bulk.screen_data(False, early)["universe"]["Ticker"])
        except (SystemExit, Exception) as exc:  # noqa: BLE001  (keep going with what we have)
            log.warning("Screener tickers skipped (early=%s): %s", early, exc)
    try:
        held = combine.build_tables(None)["all_holdings"]["Ticker"]
        found |= set(held.dropna().astype(str))
    except (SystemExit, Exception) as exc:  # noqa: BLE001
        log.warning("Filer tickers skipped: %s", exc)
    return {t.strip().upper() for t in found if t and t.strip()}


def build(retry: bool = False, limit: int | None = None) -> int:
    agent = os.environ["EDGAR_IDENTITY"]
    cache = load_map()
    todo = sorted(
        t
        for t in _wanted_tickers()
        if t not in cache or (retry and cache[t]["sector"] == UNCLASSIFIED)
    )
    if limit:
        todo = todo[:limit]
    if not todo:
        log.info("Nothing to do: %d tickers already labelled.", len(cache))
        return 0
    log.info(
        "Labelling %d tickers (about %d minutes)...", len(todo), len(todo) // 400 + 1
    )

    raw = _get_json("https://www.sec.gov/files/company_tickers.json", agent)
    if not raw:
        raise SystemExit(
            "Could not download the SEC ticker list. Check your connection."
        )
    cik_of = {}
    for row in raw.values():
        tk = str(row["ticker"]).upper()
        cik_of[tk] = int(row["cik_str"])
        cik_of.setdefault(tk.replace("-", "."), int(row["cik_str"]))
        cik_of.setdefault(tk.replace(".", "-"), int(row["cik_str"]))

    for n, tk in enumerate(todo, start=1):
        cik = cik_of.get(tk)
        sic = None
        if cik is not None:
            sub = _get_json(
                f"https://data.sec.gov/submissions/CIK{cik:010d}.json", agent
            )
            sic = (sub or {}).get("sic") or None
            time.sleep(0.12)  # stay well under the SEC's 10 requests per second
        cache[tk] = {"sic": sic, "sector": sector_for_sic(sic)}
        if n % 100 == 0:
            _save_map(cache)
            log.info("  %d / %d", n, len(todo))
    _save_map(cache)
    done = sum(1 for v in cache.values() if v["sector"] != UNCLASSIFIED)
    log.info(
        "Sector labels saved: %d of %d tickers classified -> %s",
        done,
        len(cache),
        MAP_FILE.relative_to(PROJECT_ROOT),
    )
    return 0


# --------------------------------------------------------------------------- screener view


def _verdict(up: int, down: int) -> str:
    moved = up + down
    if moved < MIN_STOCKS:
        return "Too few stocks to say"
    share = up / moved
    if share >= 0.6:
        return "Mostly gaining holders"
    if share <= 0.4:
        return "Mostly losing holders"
    return "Mixed"


def screen_sectors(universe: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per sector, one row per stock). Direction only: counts of stocks, not holders."""
    u = add_sector(universe)
    pct = (u["Change"] / u["Before"].where(u["Before"] > 0) * 100).fillna(0)
    u["Direction"] = "Flat"
    u.loc[(u["Change"] > 0) & (pct.abs() >= FLAT_PCT), "Direction"] = "Gaining holders"
    u.loc[(u["Change"] < 0) & (pct.abs() >= FLAT_PCT), "Direction"] = "Losing holders"
    rows = []
    for sector, g in u.groupby("Sector"):
        up = int((g["Direction"] == "Gaining holders").sum())
        down = int((g["Direction"] == "Losing holders").sum())
        rows.append(
            {
                "Sector": sector,
                "Stocks": len(g),
                "Gaining holders": up,
                "Losing holders": down,
                "Flat": len(g) - up - down,
                "Net (gaining - losing)": up - down,
                "Verdict": _verdict(up, down)
                if sector != UNCLASSIFIED
                else "Not classified",
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out, u
    out["_last"] = out["Sector"] == UNCLASSIFIED
    out = out.sort_values(["_last", "Net (gaining - losing)"], ascending=[True, False])
    return out.drop(columns="_last").reset_index(drop=True), u


# --------------------------------------------------------------------------- my filers view

MOVES = {
    "NEW": "Opened",
    "INCREASED": "Added to",
    "DECREASED": "Trimmed",
    "CLOSED": "Sold out",
}


def filer_sectors(holdings: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per sector, one row per move). Counts of positions and filers, shares only."""
    h = holdings
    if "PutCall" in h.columns:
        h = h[h["PutCall"].fillna("").astype(str).str.strip() == ""]  # options excluded
    h = h[h["Status"].isin(MOVES)].copy()
    h = add_sector(h)
    h["Move"] = h["Status"].map(MOVES)
    h["Stock"] = (
        h["Ticker"]
        .fillna("")
        .astype(str)
        .where(
            h["Ticker"].fillna("").astype(str) != "", h["Issuer"].fillna("").astype(str)
        )
    )
    rows = []
    for sector, g in h.groupby("Sector"):
        n = g["Move"].value_counts()
        buys = int(n.get("Opened", 0) + n.get("Added to", 0))
        sells = int(n.get("Trimmed", 0) + n.get("Sold out", 0))
        total = buys + sells
        net = buys - sells
        if sector == UNCLASSIFIED:
            verdict = "Not classified"
        elif total < MIN_STOCKS:
            verdict = "Too few moves to say"
        elif net / total >= LEAN:
            verdict = "Mostly added to"
        elif net / total <= -LEAN:
            verdict = "Mostly trimmed"
        else:
            verdict = "Mixed"
        rows.append(
            {
                "Sector": sector,
                "Opened": int(n.get("Opened", 0)),
                "Added to": int(n.get("Added to", 0)),
                "Trimmed": int(n.get("Trimmed", 0)),
                "Sold out": int(n.get("Sold out", 0)),
                "Net moves": net,
                "Filers adding": g.loc[
                    g["Move"].isin(["Opened", "Added to"]), "Filer"
                ].nunique(),
                "Filers trimming": g.loc[
                    g["Move"].isin(["Trimmed", "Sold out"]), "Filer"
                ].nunique(),
                "Verdict": verdict,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out, h
    out["_last"] = out["Sector"] == UNCLASSIFIED
    out = out.sort_values(["_last", "Net moves"], ascending=[True, False])
    return out.drop(columns="_last").reset_index(drop=True), h


SCREEN_NOTE = (
    "Counts stocks, not holders: a sector is 'gaining' when most of its stocks gained "
    "filers. Holder counts that moved less than 1% are treated as flat. Sector labels come "
    "from SEC industry codes and are coarse. No dollar amounts."
)
FILER_NOTE = (
    "Counts position moves by your own filers versus their previous quarter (shares only, "
    "no options). 'Filers adding / trimming' are distinct filers. Your filers are a small "
    "handpicked group, so this is not the market. No dollar amounts."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Label tickers with a broad sector.")
    parser.add_argument("--build", action="store_true", help="Download sector labels")
    parser.add_argument(
        "--retry", action="store_true", help="Try again for tickers that had no sector"
    )
    parser.add_argument(
        "--limit", type=int, help="Only label this many tickers (a test)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.build:
        parser.error("use --build")
    configure_environment()
    return build(args.retry, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
