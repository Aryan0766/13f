"""Fund-level counts from the SEC's Form N-PORT data sets (individual funds, not firms).

13F counts firms (Fidelity = 1). N-PORT is filed by each registered fund (mutual funds and
ETFs), so Fidelity's many funds count separately. Hedge funds and money market funds are not
included.

1. Download the quarterly ZIPs (e.g. 2026q3_nport.zip) from
   https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets
   and put them in data/sec_bulk/nport/.
2. Run "13F: Build fund (N-PORT) index" (once per new ZIP; a few minutes each).
3. "13F: Stock lookup" then shows the fund counts under the 13F table.

Only long common/preferred stock positions are kept, one row per fund, month and CUSIP.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import warnings
import zipfile
from pathlib import Path

import pandas as pd

from .bulk import BULK_DIR, INDEX_DIR, MONTHS, _norm, _ticker_to_cusips
from .processor import PROJECT_ROOT

NPORT_DIR = BULK_DIR / "nport"
NPORT_INDEX = INDEX_DIR / "nport"

KEEP_MONTHS = 15  # months of history kept in the index
PARTIAL_BELOW = 0.90  # a month with <90% of the busiest month's funds is "incomplete"
STRAGGLER_BELOW = (
    0.10  # months with <10% of the busiest month's funds are just late stragglers
)
EQUITY_CATS = {"EC", "EP"}  # N-PORT asset category: common / preferred stock
CHUNK_ROWS = 500_000

log = logging.getLogger("thirteenf")
CUSIP_OK = re.compile(r"^[0-9A-Z]{9}$")
ISIN_US_CA = re.compile(r"^(?:US|CA)[0-9A-Z]{9}[0-9]$")


# --------------------------------------------------------------------------- reading


def _member(z: zipfile.ZipFile, stem: str) -> str:
    """Find a table by name, ignoring folders, capitalization and the .tsv/.txt ending."""
    for entry in z.namelist():
        base = entry.rsplit("/", 1)[-1]
        if base.rsplit(".", 1)[0].upper() == stem:
            return entry
    shown = ", ".join(z.namelist()[:12])
    raise SystemExit(
        f"{Path(z.filename).name}: no table named {stem} inside. Found: {shown}. "
        "Is this the SEC 'Form N-PORT Data Sets' ZIP (e.g. 2026q3_nport.zip)?"
    )


def _read(z: zipfile.ZipFile, stem: str, usecols: list[str], **kwargs):
    with z.open(_member(z, stem)) as f:
        head = pd.read_csv(f, sep="\t", nrows=0)
    missing = [c for c in usecols if c not in head.columns]
    if missing:
        raise SystemExit(
            f"{Path(z.filename).name}: table {stem} has no column(s) {missing}. "
            f"Columns found: {list(head.columns)[:15]}"
        )
    f = z.open(_member(z, stem))
    return pd.read_csv(
        f,
        sep="\t",
        dtype=str,
        usecols=usecols,
        quoting=csv.QUOTE_NONE,
        encoding_errors="replace",
        **kwargs,
    )


def _dates(s: pd.Series) -> pd.Series:
    """Parse SEC dates like 31-AUG-2026 (or ISO dates) without depending on the PC's language."""
    up = s.fillna("").str.strip().str.upper()
    parts = up.str.extract(r"^(\d{1,2})-([A-Z]{3})-(\d{4})$")
    iso = parts[2] + "-" + parts[1].map(MONTHS) + "-" + parts[0].str.zfill(2)
    out = pd.to_datetime(iso, format="%Y-%m-%d", errors="coerce")
    rest = out.isna() & (up != "")
    if rest.any():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out[rest] = pd.to_datetime(up[rest], errors="coerce")
    return out


def _filings(z: zipfile.ZipFile) -> pd.DataFrame:
    """One row per fund and month: the latest filing for it in this ZIP."""
    sub = _read(
        z,
        "SUBMISSION",
        ["ACCESSION_NUMBER", "FILING_DATE", "REPORT_DATE"],
    )
    info = _read(z, "FUND_REPORTED_INFO", ["ACCESSION_NUMBER", "SERIES_ID"])
    df = sub.merge(info, on="ACCESSION_NUMBER", how="inner")
    df["report"] = _dates(df["REPORT_DATE"])
    df["filed"] = _dates(df["FILING_DATE"])
    bad = df["report"].isna() | df["SERIES_ID"].isna() | df["filed"].isna()
    if bad.any():
        log.warning("Skipping %d filings with a missing date or fund ID.", bad.sum())
    df = df[~bad].copy()
    df["month"] = df["report"].dt.strftime("%Y-%m")
    df = df.sort_values(["filed", "ACCESSION_NUMBER"])
    df = df.drop_duplicates(["SERIES_ID", "month"], keep="last")
    return df[["ACCESSION_NUMBER", "SERIES_ID", "month", "filed"]]


def _stock_rows(
    z: zipfile.ZipFile, filings: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Long common/preferred stock positions: SERIES_ID, month, CUSIP (+ counts for a sanity check).

    A holding without a usable CUSIP gets one from its ISIN when that is a US or Canadian ISIN
    (the CUSIP is the 9 characters after the country code).
    """
    keys = filings[["ACCESSION_NUMBER", "SERIES_ID", "month"]]
    wanted = [
        "ACCESSION_NUMBER",
        "HOLDING_ID",
        "ISSUER_CUSIP",
        "ASSET_CAT",
        "PAYOFF_PROFILE",
    ]
    stats = {"equity rows": 0, "with CUSIP": 0, "CUSIP from ISIN": 0, "no usable ID": 0}
    have, missing = [], []
    for chunk in _read(z, "FUND_REPORTED_HOLDING", wanted, chunksize=CHUNK_ROWS):
        chunk = chunk[chunk["ASSET_CAT"].str.strip().str.upper().isin(EQUITY_CATS)]
        chunk = chunk[
            chunk["PAYOFF_PROFILE"].fillna("").str.strip().str.upper() != "SHORT"
        ]
        chunk = chunk.merge(keys, on="ACCESSION_NUMBER", how="inner")
        cusip = chunk["ISSUER_CUSIP"].fillna("").str.strip().str.upper()
        ok = cusip.str.match(CUSIP_OK) & (cusip != "000000000")
        stats["equity rows"] += len(chunk)
        stats["with CUSIP"] += int(ok.sum())
        have.append(
            chunk.assign(CUSIP=cusip)[ok][
                ["SERIES_ID", "month", "CUSIP"]
            ].drop_duplicates()
        )
        missing.append(chunk[~ok][["HOLDING_ID", "SERIES_ID", "month"]])
    pieces = have
    lost = pd.concat(missing, ignore_index=True) if missing else pd.DataFrame()
    if not lost.empty:
        ids = set(lost["HOLDING_ID"].dropna())
        found = []
        try:
            for chunk in _read(
                z,
                "IDENTIFIERS",
                ["HOLDING_ID", "IDENTIFIER_ISIN"],
                chunksize=CHUNK_ROWS,
            ):
                chunk = chunk[chunk["HOLDING_ID"].isin(ids)]
                isin = chunk["IDENTIFIER_ISIN"].fillna("").str.strip().str.upper()
                ok = isin.str.match(ISIN_US_CA)
                found.append(
                    pd.DataFrame(
                        {
                            "HOLDING_ID": chunk["HOLDING_ID"][ok],
                            "CUSIP": isin[ok].str[2:11],
                        }
                    )
                )
        except SystemExit as exc:
            log.warning("Could not read ISINs (%s); skipping that fallback.", exc)
        if found:
            ext = pd.concat(found).drop_duplicates()
            fixed = lost.merge(ext, on="HOLDING_ID", how="inner")
            stats["CUSIP from ISIN"] = len(fixed)
            pieces = [*have, fixed[["SERIES_ID", "month", "CUSIP"]].drop_duplicates()]
        stats["no usable ID"] = len(lost) - stats["CUSIP from ISIN"]
    if not pieces:
        return pd.DataFrame(columns=["SERIES_ID", "month", "CUSIP"]), stats
    return pd.concat(pieces, ignore_index=True).drop_duplicates(), stats


# --------------------------------------------------------------------------- build


def _merge_month(month: str, series: pd.DataFrame, stocks: pd.DataFrame) -> None:
    """Add one month's data to the index; a newer filing for the same fund replaces the old one."""
    s_path = NPORT_INDEX / f"series_{month}.parquet"
    h_path = NPORT_INDEX / f"holdings_{month}.parquet"
    series = series[["SERIES_ID", "filed"]].copy()
    stocks = stocks[["SERIES_ID", "CUSIP"]]
    if s_path.exists() and h_path.exists():
        old_s = pd.read_parquet(s_path)
        old_h = pd.read_parquet(h_path)
        merged = old_s.merge(series, on="SERIES_ID", suffixes=("_old", ""))
        old_is_newer = set(
            merged.loc[merged["filed_old"] > merged["filed"], "SERIES_ID"]
        )
        replaced = set(merged["SERIES_ID"]) - old_is_newer  # new filing wins
        old_s = old_s[~old_s["SERIES_ID"].isin(replaced)]
        old_h = old_h[~old_h["SERIES_ID"].isin(replaced)]
        series = series[~series["SERIES_ID"].isin(old_is_newer)]
        stocks = stocks[~stocks["SERIES_ID"].isin(old_is_newer)]
        series = pd.concat([old_s, series])
        stocks = pd.concat([old_h, stocks])
    series.drop_duplicates("SERIES_ID", keep="last").to_parquet(s_path, index=False)
    stocks.drop_duplicates().to_parquet(h_path, index=False)


def _prune() -> None:
    months = sorted(
        p.stem.split("_", 1)[1] for p in NPORT_INDEX.glob("series_*.parquet")
    )
    for month in months[:-KEEP_MONTHS]:
        for kind in ("series", "holdings"):
            (NPORT_INDEX / f"{kind}_{month}.parquet").unlink(missing_ok=True)


def build_index() -> int:
    zips = sorted(NPORT_DIR.glob("*.zip"))
    if not zips:
        raise SystemExit(
            f"No ZIPs in {NPORT_DIR.relative_to(PROJECT_ROOT)}. Download the N-PORT data sets "
            "(see the top of nport.py) and put them there."
        )
    NPORT_INDEX.mkdir(parents=True, exist_ok=True)
    for zp in zips:
        print(f"Reading {zp.name} ...")
        with zipfile.ZipFile(zp) as z:
            filings = _filings(z)
            stocks, stats = _stock_rows(z, filings)
        for month, series in filings.groupby("month"):
            _merge_month(month, series, stocks[stocks["month"] == month])
        found = filings.groupby("month")["SERIES_ID"].nunique()
        shown = ", ".join(f"{m}: {n:,}" for m, n in found.items())
        print(f"  months in this ZIP (funds filed): {shown}")
        print("  stock holdings: " + ", ".join(f"{k} {v:,}" for k, v in stats.items()))
    _prune()
    print("\nFund index built. Months on file (funds filed / funds holding stocks):")
    for s_path in sorted(NPORT_INDEX.glob("series_*.parquet")):
        month = s_path.stem.split("_", 1)[1]
        n_all = pd.read_parquet(s_path)["SERIES_ID"].nunique()
        h = pd.read_parquet(
            NPORT_INDEX / f"holdings_{month}.parquet", columns=["SERIES_ID"]
        )
        print(f"  {month}: {n_all:,} / {h['SERIES_ID'].nunique():,}")
    return 0


# --------------------------------------------------------------------------- lookup


def has_index() -> bool:
    return any(NPORT_INDEX.glob("holdings_*.parquet"))


def fund_table(cusips: list[str]) -> pd.DataFrame | None:
    """Funds holding the stock, per month. None if the fund index has not been built."""
    months = sorted(
        p.stem.split("_", 1)[1] for p in NPORT_INDEX.glob("holdings_*.parquet")
    )
    if not months:
        return None
    filed = {
        m: pd.read_parquet(NPORT_INDEX / f"series_{m}.parquet")["SERIES_ID"].nunique()
        for m in months
    }
    biggest = max(filed.values())
    months = [m for m in months if filed[m] / biggest >= STRAGGLER_BELOW]
    rows, prev = [], None
    for m in months:
        h = pd.read_parquet(
            NPORT_INDEX / f"holdings_{m}.parquet",
            columns=["SERIES_ID", "CUSIP"],
            filters=[("CUSIP", "in", list(cusips))],
        )
        cur = set(h["SERIES_ID"])
        row = {
            "Month": m,
            "Funds": len(cur),
            "Funds filed": filed[m],
            "Complete": filed[m] / biggest >= PARTIAL_BELOW,
        }
        ok = prev is not None and row["Complete"] and prev["Complete"]
        row["Change"] = row["Funds"] - prev["Funds"] if ok else None
        row["Added"] = len(cur - prev["set"]) if ok else None
        row["Dropped"] = len(prev["set"] - cur) if ok else None
        rows.append(row)
        prev = {**row, "set": cur}
    return pd.DataFrame(rows)


def _fmt(value, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{int(value):+,}" if signed else f"{int(value):,}"


def format_block(table: pd.DataFrame | None) -> list[str]:
    if table is None:
        return [
            (
                "\nFund-level (N-PORT) counts not built yet. Put the N-PORT ZIPs in "
                "data/sec_bulk/nport/ and run '13F: Build fund (N-PORT) index'."
            )
        ]
    lines = [
        "\nINDIVIDUAL FUNDS (N-PORT: mutual funds and ETFs, each fund counted once)\n"
    ]
    lines.append(
        f"{'Month':<12}{'Funds':>8}{'Change':>9}{'Added':>8}{'Dropped':>9}{'Funds filed':>13}"
    )
    for r in table.to_dict("records"):
        mark = "" if r["Complete"] else "  DATA INCOMPLETE"
        lines.append(
            f"{r['Month']:<12}{_fmt(r['Funds']):>8}{_fmt(r['Change'], True):>9}"
            f"{_fmt(r['Added']):>8}{_fmt(r['Dropped']):>9}{_fmt(r['Funds filed']):>13}{mark}"
        )
    if not table["Complete"].all():
        late = table.loc[~table["Complete"], "Month"].tolist()
        lines.append(
            f"\n*** DATA INCOMPLETE for {', '.join(late)}: far fewer funds on file than the "
            "busiest month, so its count is too low to compare. ***"
        )
    lines.append(
        "Funds = individual fund series (not firms); long common/preferred stock only. "
        "Months with almost no funds on file are hidden. "
        "Each month is the fund's holdings at month end, public about 60 days later."
    )
    return lines


def excel_table(table: pd.DataFrame) -> pd.DataFrame:
    note = table["Complete"].map({True: "", False: "DATA INCOMPLETE"})
    cols = ["Month", "Funds", "Change", "Added", "Dropped", "Funds filed"]
    return table[cols].assign(Note=note)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fund-level counts (SEC N-PORT data sets)."
    )
    parser.add_argument("--build", action="store_true", help="Build the fund index")
    parser.add_argument("--lookup", help="Ticker or 9-character CUSIP (funds only)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.build:
        return build_index()
    if args.lookup:
        q = _norm(args.lookup)
        cusips = [q] if len(q) == 9 else _ticker_to_cusips().get(q, [])
        if not cusips:
            raise SystemExit(
                f"No CUSIP found for {args.lookup!r}. Try the 9-character CUSIP."
            )
        print("\n".join(format_block(fund_table(cusips))))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
