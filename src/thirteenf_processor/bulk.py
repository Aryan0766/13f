"""Bulk mode: count how many 13F filers hold a stock, using the SEC's Form 13F data sets.

1. Put the SEC ZIPs in data/sec_bulk/ (e.g. 01jun2026-31aug2026_form13f.zip).
2. Run "13F: Build bulk index" (once per new ZIP).
3. Run "13F: Stock lookup" and type a ticker (or a 9-character CUSIP).

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

import pandas as pd
from dotenv import load_dotenv

from .processor import PROJECT_ROOT, _autosize

BULK_DIR = PROJECT_ROOT / "data" / "sec_bulk"
INDEX_DIR = PROJECT_ROOT / "data" / "index"
LOOKUP_DIR = PROJECT_ROOT / "data" / "output" / "Lookups"

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk 13F stock lookup (SEC data sets)."
    )
    parser.add_argument(
        "--build", action="store_true", help="Build the index from data/sec_bulk/*.zip"
    )
    parser.add_argument("--lookup", help="Ticker or 9-character CUSIP to look up")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.build:
        return build_index()
    if args.lookup:
        return lookup(args.lookup)
    parser.error("use --build or --lookup TICKER")


if __name__ == "__main__":
    raise SystemExit(main())
