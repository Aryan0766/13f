"""Bulk mode: count how many 13F filers hold a stock, using the SEC's Form 13F data sets.

1. Put the SEC ZIPs in data/sec_bulk/ (e.g. 01jun2026-31aug2026_form13f.zip).
2. Run "13F: Build bulk index" (once per new ZIP).
3. Run "13F: Stock lookup" and type a ticker (or a 9-character CUSIP).
4. Run "13F: Market screener" for the stocks that gained or lost the most holders.

No dollar amounts are kept: only which filer held which CUSIP in which quarter.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from .processor import PROJECT_ROOT, _autosize

BULK_DIR = PROJECT_ROOT / "data" / "sec_bulk"
INDEX_DIR = PROJECT_ROOT / "data" / "index"
LOOKUP_DIR = PROJECT_ROOT / "data" / "output" / "Lookups"
SCREEN_DIR = PROJECT_ROOT / "data" / "output" / "Screens"

# Market screener settings (change these numbers to taste)
MIN_HOLDERS = 300  # a stock needs this many holders in at least one of the two quarters
MIN_PREV = 100  # and this many in the earlier quarter (keeps brand-new listings out)
TOP_N = 100  # rows per sheet
STREAK_MIN = 3  # quarters in a row for the "Rising streaks" sheet
# Names that look like ETFs, funds, SPACs, warrants or units. Hidden unless --include-funds.
FUND_PATTERN = re.compile(
    r"\b(?:ETF|ETFS|ETN|ISHARES|SPDR|PROSHARES|DIREXION|VANECK|WISDOMTREE|QQQ|"
    r"FUND|FUNDS|FDS|INDEX|WARRANT|WARRANTS|RIGHTS|UNITS|ACQUISITION|ACQUISITIONS)\b",
    re.IGNORECASE,
)

KEEP_PERIODS = 5  # a year of quarters plus one earlier quarter for the oldest change
MIN_FILINGS_PER_PERIOD = 20  # ignore stray late filings for old periods
PARTIAL_BELOW = 0.90  # a quarter with <90% of the biggest quarter's filers is "partial"
HOLDINGS_TYPES = ["13F-HR", "13F-HR/A"]

log = logging.getLogger("thirteenf")


def _member(z: zipfile.ZipFile, name: str) -> str:
    """Find a table inside the ZIP, ignoring folders, capitalization and filename prefixes."""
    want = name.upper()
    for entry in z.namelist():
        if entry.rsplit("/", 1)[-1].upper().endswith(want):
            return entry
    shown = ", ".join(z.namelist()[:12])
    raise SystemExit(
        f"{Path(z.filename).name}: no file named {name} inside. Found: {shown}. "
        "Is this the SEC 'Form 13F Data Sets' ZIP (not a different download)? "
        "Move it out of data/sec_bulk or tell me what it contains."
    )


def _read(z: zipfile.ZipFile, name: str, **kwargs) -> pd.DataFrame:
    with z.open(_member(z, name)) as f:
        return pd.read_csv(f, sep="\t", dtype=str, **kwargs)


MONTHS = {
    m: f"{i:02d}"
    for i, m in enumerate(
        [
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ],
        start=1,
    )
}


def _to_date(series: pd.Series) -> pd.Series:
    """Parse SEC dates like 30-JUN-2026 without depending on the computer's language."""
    parts = series.str.upper().str.extract(r"^(\d{1,2})-([A-Z]{3})-(\d{4})$")
    iso = parts[2] + "-" + parts[1].map(MONTHS) + "-" + parts[0].str.zfill(2)
    out = pd.to_datetime(iso, format="%Y-%m-%d", errors="coerce")
    if out.isna().any():
        raise SystemExit(
            "Unexpected date format in the SEC files (expected like 30-JUN-2026)."
        )
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


# --------------------------------------------------------------------------- build


def _select_filings(zips: list[Path]) -> pd.DataFrame:
    """Pick which filings count: one base report per filer+quarter, plus later 'new holdings' amendments."""
    frames = []
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            sub = _read(z, "SUBMISSION.tsv")
            cov = _read(
                z, "COVERPAGE.tsv", usecols=["ACCESSION_NUMBER", "AMENDMENTTYPE"]
            )
        frames.append(
            sub.merge(cov, on="ACCESSION_NUMBER", how="left").assign(zip=zp.name)
        )
    s = pd.concat(frames).drop_duplicates("ACCESSION_NUMBER")
    s = s[s["SUBMISSIONTYPE"].isin(HOLDINGS_TYPES)].copy()
    s["filed"] = _to_date(s["FILING_DATE"])
    s["period"] = _to_date(s["PERIODOFREPORT"])

    quarter_end = s["period"].dt.is_quarter_end
    counts = s[quarter_end].groupby("period").size()
    periods = sorted(counts[counts >= MIN_FILINGS_PER_PERIOD].index)[-KEEP_PERIODS:]
    s = s[s["period"].isin(periods)]

    s["is_add"] = s["AMENDMENTTYPE"].eq("NEW HOLDINGS")
    order = ["CIK", "period", "filed", "ACCESSION_NUMBER"]
    base = s[~s["is_add"]].sort_values(order).groupby(["CIK", "period"]).tail(1)
    adds = s[s["is_add"]].merge(
        base[["CIK", "period", "filed"]].rename(columns={"filed": "base_filed"}),
        on=["CIK", "period"],
        how="left",
    )
    adds = adds[adds["base_filed"].isna() | (adds["filed"] >= adds["base_filed"])]
    return pd.concat([base, adds])[["ACCESSION_NUMBER", "CIK", "period", "zip"]]


def build_index() -> int:
    zips = sorted(BULK_DIR.glob("*.zip"))
    if not zips:
        raise SystemExit(
            f"No ZIP files found in {BULK_DIR}. Put the SEC 13F data set ZIPs there."
        )
    log.info(
        "Reading %d ZIP file(s) from %s", len(zips), BULK_DIR.relative_to(PROJECT_ROOT)
    )

    sel = _select_filings(zips)
    periods = sorted(sel["period"].unique())
    log.info(
        "Using %d filings across quarters: %s",
        len(sel),
        ", ".join(p.strftime("%Y-%m-%d") for p in periods),
    )

    holder_parts, name_parts = [], []
    for zp in zips:
        mine = sel[sel["zip"] == zp.name]
        if mine.empty:
            continue
        with zipfile.ZipFile(zp) as z, z.open(_member(z, "INFOTABLE.tsv")) as f:
            reader = pd.read_csv(
                f,
                sep="\t",
                dtype=str,
                usecols=["ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "PUTCALL"],
                chunksize=1_000_000,
            )
            for ch in reader:
                # some filers write CUSIPs in lowercase or with spaces; normalize them
                ch["CUSIP"] = ch["CUSIP"].str.strip().str.upper()
                ch = ch[ch["ACCESSION_NUMBER"].isin(mine["ACCESSION_NUMBER"])]
                ch = ch[ch["PUTCALL"].isna()]  # equity positions only, no options
                name_parts.append(
                    ch.groupby(["CUSIP", "NAMEOFISSUER"]).size().reset_index(name="n")
                )
                ch = ch.merge(
                    mine[["ACCESSION_NUMBER", "CIK", "period"]], on="ACCESSION_NUMBER"
                )
                holder_parts.append(ch[["CIK", "period", "CUSIP"]].drop_duplicates())
        log.info("Processed %s", zp.name)

    holders = pd.concat(holder_parts).drop_duplicates()
    holders["CIK"] = holders["CIK"].astype("int64")
    holders["period"] = holders["period"].dt.strftime("%Y-%m-%d")

    names = (
        pd.concat(name_parts)
        .groupby(["CUSIP", "NAMEOFISSUER"], as_index=False)["n"]
        .sum()
    )
    names = names.sort_values("n", ascending=False).drop_duplicates("CUSIP")
    names = names.rename(columns={"NAMEOFISSUER": "Issuer"})[["CUSIP", "Issuer"]]

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    holders.to_parquet(INDEX_DIR / "holders.parquet", index=False)
    names.to_parquet(INDEX_DIR / "names.parquet", index=False)
    meta = {
        "built": datetime.now().isoformat(timespec="seconds"),
        "zips": [z.name for z in zips],
    }
    (INDEX_DIR / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info(
        "Index built: %d holder rows, %d securities -> %s",
        len(holders),
        len(names),
        INDEX_DIR,
    )
    return 0


# --------------------------------------------------------------------------- lookup


def _ticker_to_cusips() -> dict[str, list[str]]:
    load_dotenv(PROJECT_ROOT / ".env")
    os.environ.setdefault(
        "EDGAR_LOCAL_DATA_DIR", str(PROJECT_ROOT / "data" / "edgar_cache")
    )
    from edgar.reference.tickers import cusip_ticker_mapping  # bundled offline table

    ct = cusip_ticker_mapping(allow_duplicate_cusips=True).reset_index()
    ct["key"] = ct["Ticker"].map(_norm)
    return ct.groupby("key")["Cusip"].apply(list).to_dict()


def _resolve(query: str, known: set[str]) -> list[str]:
    q = _norm(query)
    if len(q) == 9 and q in known:
        return [q]
    return [c for c in _ticker_to_cusips().get(q, []) if c in known]


def _lookup_table(holders: pd.DataFrame, cusips: list[str]) -> pd.DataFrame:
    totals = holders.groupby("period")["CIK"].nunique()
    complete = totals / totals.max() >= PARTIAL_BELOW
    sets = holders[holders["CUSIP"].isin(cusips)].groupby("period")["CIK"].agg(set)

    rows, prev = [], None
    for period in sorted(totals.index):
        cur = sets.get(period, set())
        row = {"Quarter": period, "Funds": len(cur), "Complete": bool(complete[period])}
        ok = prev is not None and row["Complete"] and prev["Complete"]
        row["Change"] = row["Funds"] - prev["Funds"] if ok else None
        row["Added"] = len(cur - prev["set"]) if ok else None
        row["Dropped"] = len(prev["set"] - cur) if ok else None
        rows.append(row)
        prev = {**row, "set": cur}
    return pd.DataFrame(rows)


def _streak(table: pd.DataFrame) -> tuple[int, str]:
    done = table[table["Complete"]]
    direction, n = "", 0
    for change in reversed(done["Change"].tolist()):
        if pd.isna(change) or change == 0:
            break
        step = "up" if change > 0 else "down"
        if direction and step != direction:
            break
        direction, n = step, n + 1
    return n, direction


def _fmt(value, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{int(value):+,}" if signed else f"{int(value):,}"


def lookup(query: str) -> int:
    path = INDEX_DIR / "holders.parquet"
    if not path.exists():
        raise SystemExit("No index yet. Run '13F: Build bulk index' first.")
    holders = pd.read_parquet(path)
    names = pd.read_parquet(INDEX_DIR / "names.parquet").set_index("CUSIP")["Issuer"]

    cusips = _resolve(query, set(holders["CUSIP"].unique()))
    if not cusips:
        raise SystemExit(
            f"No holdings found for {query!r}. Check the ticker, or type the 9-character CUSIP. "
            "(Options, ETFs without 13F holders and brand-new listings may not appear.)"
        )

    table = _lookup_table(holders, cusips)
    issuer = names.get(cusips[0], "")
    streak, direction = _streak(table)

    lines = [f"\n{query.upper()}  {issuer}   (CUSIP {', '.join(cusips)})\n"]
    lines.append(f"{'Quarter':<12}{'Funds':>8}{'Change':>9}{'Added':>8}{'Dropped':>9}")
    for r in table.itertuples():
        mark = "" if r.Complete else "  * partial"
        lines.append(
            f"{r.Quarter:<12}{_fmt(r.Funds):>8}{_fmt(r.Change, True):>9}"
            f"{_fmt(r.Added):>8}{_fmt(r.Dropped):>9}{mark}"
        )
    done = table[table["Complete"]]
    if not done.empty:
        last = done.iloc[-1]
        lines.append(
            f"\nLatest complete quarter ({last['Quarter']}): {int(last['Funds']):,} funds held it."
        )
        if streak >= 2:
            lines.append(f"Fund count has gone {direction} {streak} quarters in a row.")
    if not table["Complete"].all():
        lines.append(
            "* partial = filings for that quarter are still missing from your ZIPs, so its count is low."
        )
    lines.append(
        "Counts are 13F filers (institutional managers), not individual mutual funds."
    )
    print("\n".join(lines))

    LOOKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = LOOKUP_DIR / f"{_norm(query)}_funds.xlsx"
    export = table.drop(columns=["Complete"]).assign(
        Note=table["Complete"].map({True: "", False: "partial"})
    )
    try:
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            export.to_excel(xw, sheet_name="Funds holding", index=False)
            _autosize(xw.sheets["Funds holding"])
        print(f"Saved: {out.relative_to(PROJECT_ROOT)}")
    except PermissionError:
        print(f"(Could not save {out.name}; close it in Excel and run again.)")
    return 0


def _ticker_by_cusip() -> dict[str, str]:
    load_dotenv(PROJECT_ROOT / ".env")
    os.environ.setdefault(
        "EDGAR_LOCAL_DATA_DIR", str(PROJECT_ROOT / "data" / "edgar_cache")
    )
    try:
        from edgar.reference.tickers import cusip_ticker_mapping

        ct = cusip_ticker_mapping(allow_duplicate_cusips=True).reset_index()
    except Exception as exc:  # noqa: BLE001  (the screener still works without tickers)
        log.warning("Could not load the ticker table (%s); showing names only.", exc)
        return {}
    ct["Cusip"] = ct["Cusip"].str.strip().str.upper()
    return ct.drop_duplicates("Cusip").set_index("Cusip")["Ticker"].to_dict()


def _screen_periods(holders: pd.DataFrame) -> tuple[str, str, list[str]]:
    """Return (latest complete quarter, the quarter before it, unbroken run of complete quarters)."""
    totals = holders.groupby("period")["CIK"].nunique().sort_index()
    complete = totals / totals.max() >= PARTIAL_BELOW
    periods = list(totals.index)
    done = [p for p in periods if complete[p]]
    if not done:
        raise SystemExit("No complete quarter in the index. Add more ZIPs and rebuild.")
    latest = done[-1]
    pos = periods.index(latest)
    if pos == 0:
        raise SystemExit(
            f"Only one quarter ({latest}) is in the index, so there is nothing to compare. "
            "Add the ZIP(s) for earlier quarters to data/sec_bulk/ and run 'Build bulk index'."
        )
    if not complete[periods[pos - 1]]:
        raise SystemExit(
            f"The quarter before {latest} ({periods[pos - 1]}) is only partly covered, so a "
            "comparison would be misleading. Add the ZIP that holds its filings "
            "to data/sec_bulk/ and run 'Build bulk index'."
        )
    run, i = [], pos
    while i >= 0 and complete[periods[i]]:
        run.insert(0, periods[i])
        i -= 1
    return latest, periods[pos - 1], run


def _signed_streaks(counts: pd.DataFrame) -> pd.Series:
    """Quarters in a row the count moved the same way as its latest move (+3 = up 3, -2 = down 2)."""
    sign = np.sign(counts.diff(axis=1).iloc[:, 1:].to_numpy())
    if sign.shape[1] == 0:
        return pd.Series(0, index=counts.index)
    last = sign[:, -1]
    alive = last != 0
    streak = np.zeros(len(counts), dtype=int)
    for k in range(sign.shape[1] - 1, -1, -1):
        alive = alive & (sign[:, k] == last)
        streak += alive
    return pd.Series((streak * last).astype(int), index=counts.index)


def screen(include_funds: bool = False) -> int:
    path = INDEX_DIR / "holders.parquet"
    if not path.exists():
        raise SystemExit("No index yet. Run '13F: Build bulk index' first.")
    holders = pd.read_parquet(path)
    names = pd.read_parquet(INDEX_DIR / "names.parquet").set_index("CUSIP")["Issuer"]
    latest, prev, run = _screen_periods(holders)

    counts = (
        holders[holders["period"].isin(run)]
        .groupby(["CUSIP", "period"])
        .size()
        .unstack(fill_value=0)[run]
    )
    now_set = holders.loc[holders["period"] == latest, ["CIK", "CUSIP"]]
    prev_set = holders.loc[holders["period"] == prev, ["CIK", "CUSIP"]]
    keys = ["CIK", "CUSIP"]
    gone = prev_set.merge(now_set, on=keys, how="left", indicator=True)
    new = now_set.merge(prev_set, on=keys, how="left", indicator=True)
    added = new[new["_merge"] == "left_only"].groupby("CUSIP").size()
    dropped = gone[gone["_merge"] == "left_only"].groupby("CUSIP").size()

    t = pd.DataFrame({"Before": counts[prev], "Now": counts[latest]})
    t["Change"] = t["Now"] - t["Before"]
    t["Change %"] = (t["Change"] / t["Before"].where(t["Before"] > 0) * 100).round(1)
    t["Added"] = added.reindex(t.index).fillna(0).astype(int)
    t["Dropped"] = dropped.reindex(t.index).fillna(0).astype(int)
    t["Streak"] = _signed_streaks(counts)
    t["Issuer"] = names.reindex(t.index).fillna("")
    t["Ticker"] = pd.Series(_ticker_by_cusip()).reindex(t.index).fillna("")
    t["Type guess"] = (
        t["Issuer"]
        .str.contains(FUND_PATTERN)
        .map({True: "ETF / fund / SPAC?", False: ""})
    )
    t = t.reset_index().rename(columns={"index": "CUSIP"})

    t = t[(t["Before"] >= MIN_PREV) & (t[["Before", "Now"]].max(axis=1) >= MIN_HOLDERS)]
    hidden = 0
    if not include_funds:
        hidden = int((t["Type guess"] != "").sum())
        t = t[t["Type guess"] == ""]
    cols = ["Ticker", "Issuer", "CUSIP", "Before", "Now", "Change", "Change %"]
    cols += ["Added", "Dropped", "Streak"] + (["Type guess"] if include_funds else [])
    t = t[cols]

    gainers = t[t["Change"] > 0].sort_values(["Change", "Change %"], ascending=False)
    losers = t[t["Change"] < 0].sort_values(["Change", "Change %"])
    streaks = t[t["Streak"] >= STREAK_MIN].sort_values(
        ["Streak", "Change"], ascending=False
    )
    gainers, losers, streaks = (x.head(TOP_N) for x in (gainers, losers, streaks))

    print(
        f"\nHolder-count screen: {latest} vs {prev}   "
        f"({len(t):,} stocks screened, {hidden:,} ETFs/funds/SPACs hidden)\n"
    )
    show = gainers.head(25)
    print(
        f"{'Ticker':<8}{'Issuer':<32}{'Before':>8}{'Now':>8}{'Change':>8}{'%':>8}{'Streak':>8}"
    )
    for r in show.to_dict("records"):
        print(
            f"{r['Ticker'][:7]:<8}{r['Issuer'][:30]:<32}{r['Before']:>8,}{r['Now']:>8,}"
            f"{r['Change']:>+8,}{r['Change %']:>+8.1f}{r['Streak']:>+8}"
        )
    print(
        "\nTop 25 gainers shown. The Excel file has the top gainers, top losers and "
        f"stocks rising {STREAK_MIN}+ quarters in a row.\n"
        "Counts are 13F filers (institutional managers), not individual mutual funds."
    )

    note_lines = [
        f"Compares {latest} with {prev}. Counts are 13F filers holding the shares.",
        f"Kept: at least {MIN_PREV} holders in {prev} and {MIN_HOLDERS} in one of the two quarters.",
        "Change = Now - Before. Added / Dropped = filers that started / stopped holding it.",
        (
            "Streak = quarters in a row the count moved the same way (+ up, - down); "
            f"the 'Rising' sheet needs +{STREAK_MIN} or more."
        ),
        (
            "ETFs, funds, SPACs, warrants and units are hidden by a name guess "
            "(run with --include-funds to see them)."
        ),
        (
            "Index rebalances, splits and CUSIP changes can move a count without real interest. "
            "A holder count is not a buy or sell signal."
        ),
    ]
    notes = pd.DataFrame({"Notes": note_lines})
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    out = SCREEN_DIR / f"holder_screen_{latest}.xlsx"
    try:
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            for title, frame in (
                ("Gainers", gainers),
                ("Losers", losers),
                (f"Rising {STREAK_MIN}+ qtrs", streaks),
            ):
                frame.to_excel(xw, sheet_name=title, index=False)
                _autosize(xw.sheets[title])
                xw.sheets[title].freeze_panes = "A2"
            notes.to_excel(xw, sheet_name="Notes", index=False)
            _autosize(xw.sheets["Notes"])
        print(f"Saved: {out.relative_to(PROJECT_ROOT)}")
    except PermissionError:
        print(f"(Could not save {out.name}; close it in Excel and run again.)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk 13F stock lookup and screener (SEC data sets)."
    )
    parser.add_argument(
        "--build", action="store_true", help="Build the index from data/sec_bulk/*.zip"
    )
    parser.add_argument("--lookup", help="Ticker or 9-character CUSIP to look up")
    parser.add_argument(
        "--screen", action="store_true", help="Stocks with the biggest holder changes"
    )
    parser.add_argument(
        "--include-funds",
        action="store_true",
        help="With --screen: keep ETFs, funds, SPACs, warrants and units",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.build:
        return build_index()
    if args.lookup:
        return lookup(args.lookup)
    if args.screen:
        return screen(args.include_funds)
    parser.error("use --build, --lookup TICKER or --screen")


if __name__ == "__main__":
    raise SystemExit(main())
