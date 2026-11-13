"""Live mode: fetch the newest quarter's 13F filings straight from EDGAR.

The SEC data set for a quarter is posted weeks after the filing deadline. This lets you
pull the filings that are already in, a little at a time, and check them later.

1. Run "13F: Live fetch" now and then. Each run only downloads filings it does not have yet.
2. Stock lookup and the market screener include the live quarter automatically (marked "live").
3. When the SEC data set for that quarter arrives: put the ZIP in data/sec_bulk/, run
   "13F: Build bulk index", then run "13F: Live vs data set check". The data set takes over.

Same rules as the bulk index: shares only (no options), one report per filer and quarter
(the latest one, plus 'new holdings' amendments), CUSIPs uppercased.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date, timedelta

import pandas as pd

from .processor import PROJECT_ROOT, _autosize, configure_environment

LIVE_DIR = PROJECT_ROOT / "data" / "live"
INDEX_DIR = PROJECT_ROOT / "data" / "index"
CHECK_DIR = PROJECT_ROOT / "data" / "output" / "CrossChecks"

FORMS = ["13F-HR", "13F-HR/A"]
DEADLINE_DAYS = 45
CHECKPOINT_EVERY = 100  # save progress after this many filings (Ctrl+C is safe)
MAX_FAILURES_IN_A_ROW = 25  # then stop: probably no internet or a blocked identity
MAX_DIFF_ROWS = 5000  # rows listed in the cross-check Excel

FILING_COLUMNS = [
    "ACCESSION_NUMBER",
    "CIK",
    "form",
    "period",
    "filed",
    "AMENDMENTTYPE",
    "rows",
]

log = logging.getLogger("thirteenf")


# --------------------------------------------------------------------------- helpers


def _default_period(today: date | None = None) -> str:
    """The most recent quarter end before today."""
    today = today or date.today()
    ends = [
        date(y, m, d)
        for y in (today.year - 1, today.year)
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
    ]
    return max(e for e in ends if e < today).isoformat()


def _due_date(period: str) -> date:
    """45 days after quarter end; a weekend deadline moves to Monday."""
    due = date.fromisoformat(period) + timedelta(days=DEADLINE_DAYS)
    while due.weekday() >= 5:
        due += timedelta(days=1)
    return due


def _filing_quarters(period: str, today: date | None = None) -> list[tuple[int, int]]:
    """Calendar (year, quarter) pairs in which filings for this period can appear."""
    today = today or date.today()
    start = date.fromisoformat(period) + timedelta(days=1)
    end = min(today, start + timedelta(days=135))
    pairs, day = [], start
    while day <= end:
        pair = (day.year, (day.month - 1) // 3 + 1)
        if pair not in pairs:
            pairs.append(pair)
        day += timedelta(days=7)
    last = (end.year, (end.month - 1) // 3 + 1)
    if last not in pairs:
        pairs.append(last)
    return pairs


def _empty_filings() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ACCESSION_NUMBER": pd.Series(dtype="object"),
            "CIK": pd.Series(dtype="int64"),
            "form": pd.Series(dtype="object"),
            "period": pd.Series(dtype="object"),
            "filed": pd.Series(dtype="object"),
            "AMENDMENTTYPE": pd.Series(dtype="object"),
            "rows": pd.Series(dtype="int64"),
        }
    )


def _empty_holdings() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ACCESSION_NUMBER": pd.Series(dtype="object"),
            "CUSIP": pd.Series(dtype="object"),
        }
    )


def _load_state(period: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    folder = LIVE_DIR / period
    fp, hp = folder / "filings.parquet", folder / "holdings.parquet"
    if fp.exists() and hp.exists():
        return pd.read_parquet(fp), pd.read_parquet(hp)
    return _empty_filings(), _empty_holdings()


def _save_state(period: str, filings: pd.DataFrame, holdings: pd.DataFrame) -> None:
    folder = LIVE_DIR / period
    folder.mkdir(parents=True, exist_ok=True)
    for name, frame in (("holdings", holdings), ("filings", filings)):
        tmp = folder / f"{name}.parquet.tmp"
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, folder / f"{name}.parquet")  # never leaves a half-written file


def _select(filings: pd.DataFrame, period: str) -> pd.DataFrame:
    """Same rule as the bulk index: latest base report per filer + 'new holdings' amendments."""
    f = filings[filings["period"] == period].copy()
    f["is_add"] = f["AMENDMENTTYPE"].eq("NEW HOLDINGS")
    order = ["CIK", "filed", "ACCESSION_NUMBER"]
    base = f[~f["is_add"]].sort_values(order).groupby("CIK").tail(1)
    adds = f[f["is_add"]].merge(
        base[["CIK", "filed"]].rename(columns={"filed": "base_filed"}),
        on="CIK",
        how="left",
    )
    adds = adds[adds["base_filed"].isna() | (adds["filed"] >= adds["base_filed"])]
    return pd.concat([base, adds.drop(columns=["base_filed"])])


def _live_holders(
    period: str, filings: pd.DataFrame, holdings: pd.DataFrame
) -> pd.DataFrame:
    """Rows shaped like the bulk index: CIK, period, CUSIP."""
    sel = _select(filings, period)[["ACCESSION_NUMBER", "CIK", "period"]]
    out = holdings.merge(sel, on="ACCESSION_NUMBER")[["CIK", "period", "CUSIP"]]
    out = out.drop_duplicates()
    out["CIK"] = out["CIK"].astype("int64")
    return out


def load_live_holders(skip_periods: set[str]) -> pd.DataFrame | None:
    """Live holders for quarters the bulk index does not have yet (used by lookup/screener)."""
    frames = []
    if not LIVE_DIR.exists():
        return None
    for folder in sorted(LIVE_DIR.iterdir()):
        if not folder.is_dir() or folder.name in skip_periods:
            continue
        filings, holdings = _load_state(folder.name)
        if len(filings) and len(holdings):
            frames.append(_live_holders(folder.name, filings, holdings))
    return pd.concat(frames, ignore_index=True) if frames else None


# --------------------------------------------------------------------------- EDGAR access


def _list_filings(period: str) -> pd.DataFrame:
    """Every 13F-HR / 13F-HR/A in EDGAR's index filed after the quarter end."""
    from edgar import get_filings

    start = date.fromisoformat(period) + timedelta(days=1)
    frames = []
    for year, quarter in _filing_quarters(period):
        got = get_filings(year=year, quarter=quarter, form=FORMS)
        if got is not None and len(got):
            frames.append(got.to_pandas())
    if not frames:
        return pd.DataFrame(
            columns=["form", "company", "cik", "filing_date", "accession_number"]
        )
    df = pd.concat(frames, ignore_index=True)
    df = df[df["form"].isin(FORMS)].copy()
    df["filing_date"] = pd.to_datetime(df["filing_date"]).dt.date
    df = df[df["filing_date"] >= start].drop_duplicates("accession_number")
    return df.sort_values(["filing_date", "accession_number"]).reset_index(drop=True)


def _open(row):
    """Open one filing with edgartools (kept separate so tests can replace it)."""
    from edgar import Filing
    from edgar.thirteenf import ThirteenF

    filing = Filing(
        cik=int(row.cik),
        company=row.company,
        form=row.form,
        filing_date=row.filing_date.isoformat(),
        accession_no=row.accession_number,
    )
    return ThirteenF(filing)


def _fetch_filing(row, period: str) -> tuple[dict, pd.DataFrame | None]:
    doc = _open(row)
    reported = doc.report_period
    found = pd.to_datetime(reported).strftime("%Y-%m-%d") if reported else ""
    meta = {
        "ACCESSION_NUMBER": row.accession_number,
        "CIK": int(row.cik),
        "form": row.form,
        "period": found,
        "filed": row.filing_date.isoformat(),
        "AMENDMENTTYPE": doc.amendment_type or "",
        "rows": 0,
    }
    if (
        found != period
    ):  # a late filing for another quarter: remember it, skip the download
        return meta, None

    raw = doc.raw_infotable
    if raw is None or raw.empty:
        return meta, _empty_holdings()
    put_call = raw["PutCall"].fillna("").astype(str).str.strip()
    cusips = raw.loc[put_call == "", "Cusip"].astype(str).str.strip().str.upper()
    cusips = cusips[cusips != ""].drop_duplicates()
    meta["rows"] = len(cusips)
    return meta, pd.DataFrame(
        {"ACCESSION_NUMBER": row.accession_number, "CUSIP": cusips.to_numpy()}
    )


# --------------------------------------------------------------------------- fetch


def _previous_total(period: str) -> tuple[str, int] | None:
    path = INDEX_DIR / "holders.parquet"
    if not path.exists():
        return None
    h = pd.read_parquet(path, columns=["CIK", "period"])
    earlier = sorted(p for p in h["period"].unique() if p < period)
    if not earlier:
        return None
    return earlier[-1], int(h.loc[h["period"] == earlier[-1], "CIK"].nunique())


def _print_status(
    period: str, filings: pd.DataFrame, holdings: pd.DataFrame, newest=None
) -> None:
    n = (
        _live_holders(period, filings, holdings)["CIK"].nunique()
        if len(holdings)
        else 0
    )
    line = f"\nLive {period}: {n:,} filers fetched so far."
    prev = _previous_total(period)
    if prev and prev[1]:
        line += (
            f" That is about {n / prev[1]:.0%} of the {prev[1]:,} filers in {prev[0]}."
        )
    print(line)
    due = _due_date(period)
    print(
        f"Filing deadline: {due:%a %Y-%m-%d}. Most filings land in the last few days."
    )
    if newest:
        print(f"Newest filing seen in EDGAR's list: {newest}.")
        if (date.today() - newest).days > 3 and date.today() <= due:
            print(
                "  (That looks old. EDGAR's index can lag a day or two; try again later.)"
            )
    print(
        "Stock lookup and the screener already include it (marked live). "
        "Re-run this weekly; it only downloads new filings."
    )


def fetch(period: str | None = None) -> int:
    configure_environment()
    period = period or _default_period()
    filings, holdings = _load_state(period)
    seen = set(filings["ACCESSION_NUMBER"])

    log.info("Live quarter: %s. Reading EDGAR's filing list...", period)
    listed = _list_filings(period)
    newest = listed["filing_date"].max() if len(listed) else None
    todo = listed[~listed["accession_number"].isin(seen)]
    log.info(
        "%d 13F filings listed, %d already fetched, %d new.",
        len(listed),
        len(seen),
        len(todo),
    )

    new_f: list[dict] = []
    new_h: list[pd.DataFrame] = []
    failed: list[str] = []
    in_a_row = 0

    def flush() -> None:
        nonlocal filings, holdings
        if new_f:
            add = pd.DataFrame(new_f, columns=FILING_COLUMNS)
            filings = pd.concat([filings, add], ignore_index=True)
        if new_h:
            holdings = pd.concat([holdings, *new_h], ignore_index=True)
        new_f.clear()
        new_h.clear()
        _save_state(period, filings, holdings)

    started = time.time()
    try:
        for i, row in enumerate(todo.itertuples(index=False), start=1):
            try:
                meta, got = _fetch_filing(row, period)
            except Exception as exc:
                failed.append(f"{row.company} ({row.accession_number}): {exc}")
                in_a_row += 1
                if in_a_row >= MAX_FAILURES_IN_A_ROW:
                    raise SystemExit(
                        f"{in_a_row} filings in a row failed (last: {exc}). Check your internet "
                        "connection and EDGAR_IDENTITY in .env, then run again. Progress is saved."
                    ) from exc
                continue
            in_a_row = 0
            new_f.append(meta)
            if got is not None:
                new_h.append(got)
            if i % CHECKPOINT_EVERY == 0:
                flush()
                per = (time.time() - started) / i
                log.info(
                    "Fetched %d of %d (about %d min left)",
                    i,
                    len(todo),
                    round(per * (len(todo) - i) / 60),
                )
    finally:
        flush()  # also runs on Ctrl+C or an error, so nothing fetched is lost

    if failed:
        print(f"\n{len(failed)} filings failed and will be retried next run, e.g.:")
        for line in failed[:5]:
            print("  -", line)
    _print_status(period, filings, holdings, newest)
    return 0


# --------------------------------------------------------------------------- cross-check


def compare(period: str | None = None) -> int:
    path = INDEX_DIR / "holders.parquet"
    if not path.exists():
        raise SystemExit("No bulk index yet. Run '13F: Build bulk index' first.")
    if period is None:
        folders = (
            sorted(p.name for p in LIVE_DIR.iterdir()) if LIVE_DIR.exists() else []
        )
        if not folders:
            raise SystemExit("No live data yet. Run '13F: Live fetch' first.")
        period = folders[-1]
    bulk = pd.read_parquet(path)
    if period not in set(bulk["period"].unique()):
        raise SystemExit(
            f"The SEC data set has no {period} quarter in your index yet. Put its ZIP in "
            "data/sec_bulk/, run '13F: Build bulk index', then run this again."
        )
    filings, holdings = _load_state(period)
    if not len(holdings):
        raise SystemExit(f"No live data for {period}. Run '13F: Live fetch' first.")

    live = _live_holders(period, filings, holdings)
    sec = bulk.loc[bulk["period"] == period, ["CIK", "CUSIP"]]
    live_f, sec_f = set(live["CIK"]), set(sec["CIK"])
    common = live_f & sec_f
    pairs = ["CIK", "CUSIP"]
    a = live.loc[live["CIK"].isin(common), pairs]
    b = sec.loc[sec["CIK"].isin(common), pairs]
    both = a.merge(b, on=pairs, how="outer", indicator=True)
    only_live = both[both["_merge"] == "left_only"].drop(columns="_merge")
    only_sec = both[both["_merge"] == "right_only"].drop(columns="_merge")
    matched = int((both["_merge"] == "both").sum())
    compared = len(both)

    names_path = INDEX_DIR / "names.parquet"
    names = (
        pd.read_parquet(names_path).set_index("CUSIP")["Issuer"]
        if names_path.exists()
        else pd.Series(dtype=str)
    )
    counts = (
        pd.DataFrame(
            {
                "Live": a.groupby("CUSIP").size(),
                "SEC data set": b.groupby("CUSIP").size(),
            }
        )
        .fillna(0)
        .astype(int)
    )
    counts["Difference"] = counts["Live"] - counts["SEC data set"]
    counts = counts[counts["Difference"] != 0]
    counts["Issuer"] = names.reindex(counts.index).fillna("")
    counts = counts.reindex(
        counts["Difference"].abs().sort_values(ascending=False).index
    ).reset_index(names="CUSIP")

    summary = pd.DataFrame(
        {
            "Item": [
                "Quarter compared",
                "Filers in both sources",
                "Filers only in the live fetch",
                "Filers only in the SEC data set (not fetched yet, or filed too late for the fetch)",
                "Positions compared (filers in both)",
                "Positions identical in both",
                "In live only",
                "In SEC data set only",
                "Stocks whose counts differ (filers in both)",
            ],
            "Value": [
                period,
                len(common),
                len(live_f - sec_f),
                len(sec_f - live_f),
                compared,
                matched,
                len(only_live),
                len(only_sec),
                len(counts),
            ],
        }
    )
    print(f"\nLive vs SEC data set, quarter {period}\n")
    print(summary.to_string(index=False, header=False))
    if compared and not len(only_live) and not len(only_sec):
        print("\nExact match on every position for the filers in both sources.")
    elif compared:
        print(
            f"\n{matched / compared:.2%} of positions match. See the Excel file for the "
            "differences (often an amendment filed after your last fetch)."
        )

    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    out = CHECK_DIR / f"live_vs_dataset_{period}.xlsx"
    who = pd.concat(
        [
            pd.DataFrame({"CIK": sorted(live_f - sec_f), "Only in": "live fetch"}),
            pd.DataFrame({"CIK": sorted(sec_f - live_f), "Only in": "SEC data set"}),
        ],
        ignore_index=True,
    )
    diffs = pd.concat(
        [
            only_live.assign(**{"Only in": "live fetch"}),
            only_sec.assign(**{"Only in": "SEC data set"}),
        ],
        ignore_index=True,
    ).head(MAX_DIFF_ROWS)
    try:
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            sheets = (
                ("Summary", summary),
                ("Stock counts that differ", counts),
                ("Position differences", diffs),
                ("Filers in one source only", who),
            )
            for title, frame in sheets:
                frame.to_excel(xw, sheet_name=title, index=False)
                _autosize(xw.sheets[title])
        print(f"Saved: {out.relative_to(PROJECT_ROOT)}")
    except PermissionError:
        print(f"(Could not save {out.name}; close it in Excel and run again.)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the newest quarter's 13F filings from EDGAR and cross-check them."
    )
    parser.add_argument("--fetch", action="store_true", help="Download new filings")
    parser.add_argument(
        "--compare", action="store_true", help="Compare with the SEC data set"
    )
    parser.add_argument(
        "--period",
        help="Quarter end as YYYY-MM-DD (default: the latest quarter end before today)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.fetch:
        return fetch(args.period)
    if args.compare:
        return compare(args.period)
    parser.error("use --fetch or --compare")


if __name__ == "__main__":
    raise SystemExit(main())
