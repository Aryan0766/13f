"""Fetch 13F-HR filings for a filer and export an Excel workbook per quarter.

Each workbook has three tabs: Holdings (with quarter-over-quarter changes),
New & Closed (positions opened / fully sold since last quarter) and Summary.

Run from VS Code (Run and Debug panel) or:  python -m thirteenf_processor --filer BRK-A
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from openpyxl.styles import Font

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
STATE_FILE = PROJECT_ROOT / "data" / "state.json"
FILERS_FILE = PROJECT_ROOT / "filers.json"
TRACKER_FILE = OUTPUT_DIR / "Tracker.xlsx"
FILING_DEADLINE_DAYS = 45  # 13F is due 45 days after quarter end

log = logging.getLogger("thirteenf")

HOLDINGS_COLUMNS = [
    "Period",
    "PrevPeriod",
    "Status",
    "Ticker",
    "Issuer",
    "Cusip",
    "PutCall",
    "Shares",
    "PrevShares",
    "ShareChange",
    "ShareChangePct",
    "Value",
    "PrevValue",
    "ValueChange",
    "ValueChangePct",
]
ID_COLUMNS = ["Ticker", "Issuer", "Cusip", "PutCall"]
TOP_N = 10


def configure_environment() -> None:
    """Load .env and make sure edgartools stays inside the project folder.

    Must run BEFORE `import edgar`, because edgartools reads these variables on import.
    """
    env_file = PROJECT_ROOT / ".env"
    load_dotenv(env_file)

    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity or "example.com" in identity:
        sys.exit(
            f"EDGAR_IDENTITY is not set (looked for {env_file}, "
            f"exists: {env_file.exists()}). Put your real name and email in it."
        )

    cache = Path(os.environ.get("EDGAR_LOCAL_DATA_DIR", "data/edgar_cache"))
    if not cache.is_absolute():
        cache = PROJECT_ROOT / cache
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(cache)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


# --------------------------------------------------------------------------- tables


def build_table(report) -> tuple[pd.DataFrame, bool]:
    """Holdings plus quarter-over-quarter changes. Voting columns are dropped.

    Returns (table, has_previous). Uses edgartools' compare_holdings(): one row per
    security in either quarter; positions sold out entirely appear as CLOSED.
    """
    period = str(report.report_period)
    comparison = report.compare_holdings()

    if comparison is None:
        table = report.holdings.rename(columns={"SharesPrnAmount": "Shares"}).copy()
        table.insert(0, "PrevPeriod", "")
        table.insert(0, "Period", period)
        has_previous = False
    else:
        table = comparison.data.copy()
        table.insert(0, "PrevPeriod", str(comparison.previous_period))
        table.insert(0, "Period", period)
        has_previous = True

    table = table.reindex(columns=[c for c in HOLDINGS_COLUMNS if c in table.columns])
    return table, has_previous


def is_option(table: pd.DataFrame) -> pd.Series:
    """True for call/put rows. Their 'shares' are the underlying shares of the contracts."""
    if "PutCall" not in table.columns:
        return pd.Series(False, index=table.index)
    return table["PutCall"].fillna("").astype(str).str.strip() != ""


def build_new_closed(table: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    opt = is_option(table)  # shares first, calls/puts after, each biggest first
    new = (
        table[table["Status"] == "NEW"]
        .assign(_opt=opt)
        .sort_values(["_opt", "Value"], ascending=[True, False])
    )
    closed = (
        table[table["Status"] == "CLOSED"]
        .assign(_opt=opt)
        .sort_values(["_opt", "PrevValue"], ascending=[True, False])
    )
    ids = [c for c in ID_COLUMNS if c in table.columns]
    return (
        new[ids + ["Shares", "Value"]].reset_index(drop=True),
        closed[ids + ["PrevShares", "PrevValue"]].reset_index(drop=True),
    )


def build_top_changes(
    table: pd.DataFrame, options: bool = False
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Top increases / decreases among positions held in both quarters.

    Ranked by estimated trade size = share change x this quarter's implied price,
    so price moves don't distort the ranking. Shares and options are listed
    separately (options = calls/puts, whose 'shares' are the underlying shares).
    """
    both = table[
        table["Status"].isin(["INCREASED", "DECREASED"]) & (is_option(table) == options)
    ].copy()
    price = (both["Value"] / both["Shares"]).replace([np.inf, -np.inf], np.nan)
    both["EstTradeValue"] = both["ShareChange"] * price
    cols = [c for c in ["Ticker", "Issuer", "PutCall"] if c in both.columns] + [
        "Shares",
        "PrevShares",
        "ShareChange",
        "ShareChangePct",
        "EstTradeValue",
    ]
    top_up = both[both["Status"] == "INCREASED"].nlargest(TOP_N, "EstTradeValue")
    top_down = both[both["Status"] == "DECREASED"].nsmallest(TOP_N, "EstTradeValue")
    return top_up[cols].reset_index(drop=True), top_down[cols].reset_index(drop=True)


def summary_rows(table: pd.DataFrame, meta: dict, has_previous: bool) -> list[tuple]:
    rows: list[tuple] = [
        ("Manager", meta["manager"]),
        ("Filing date", str(meta["filing_date"])),
        ("Accession number", meta["accession"]),
        ("Report period", str(table["Period"].iloc[0]) if len(table) else ""),
    ]
    if meta.get("total_value"):
        rows.append(
            (
                "Total value reported by SEC (this quarter, includes options)",
                float(meta["total_value"]),
            )
        )

    if not has_previous:
        rows.append(
            ("Note", "No previous quarter filing found, so no changes were computed.")
        )
        rows.append(("Positions", len(table)))
        return rows

    opt = is_option(table)
    stocks, options = (
        table[~opt],
        table[opt],
    )  # kept apart so option books don't skew it
    counts = stocks["Status"].value_counts()
    value_now = float(stocks["Value"].sum())
    value_prev = float(stocks["PrevValue"].sum())
    rows += [
        ("Compared with", str(table["PrevPeriod"].iloc[0])),
        ("Stocks only", "Calls and puts are counted separately, below."),
        ("", ""),
        ("Positions this quarter", int(stocks["Shares"].notna().sum())),
        ("Positions last quarter", int(stocks["PrevShares"].notna().sum())),
        ("New positions", int(counts.get("NEW", 0))),
        ("Closed positions", int(counts.get("CLOSED", 0))),
        ("Increased positions", int(counts.get("INCREASED", 0))),
        ("Decreased positions", int(counts.get("DECREASED", 0))),
        ("Unchanged positions", int(counts.get("UNCHANGED", 0))),
        ("", ""),
        ("Sum of position values, this quarter", value_now),
        ("Sum of position values, last quarter", value_prev),
        ("Change %", (value_now / value_prev - 1) * 100 if value_prev else None),
    ]
    if not options.empty:
        oc = options["Status"].value_counts()
        rows += [
            ("", ""),
            (
                "Options (calls and puts)",
                "Value is the underlying shares' value, not money spent.",
            ),
            ("Option positions this quarter", int(options["Shares"].notna().sum())),
            ("Option positions last quarter", int(options["PrevShares"].notna().sum())),
            ("New option positions", int(oc.get("NEW", 0))),
            ("Closed option positions", int(oc.get("CLOSED", 0))),
            ("Increased option positions", int(oc.get("INCREASED", 0))),
            ("Decreased option positions", int(oc.get("DECREASED", 0))),
            ("Option underlying value, this quarter", float(options["Value"].sum())),
            (
                "Option underlying value, last quarter",
                float(options["PrevValue"].sum()),
            ),
        ]
    return rows


# --------------------------------------------------------------------------- Excel


def _number_format(column: str) -> str | None:
    if column.endswith("Pct"):
        return '0.0"%"'
    if "Value" in column:
        return "$#,##0"
    if "Shares" in column or column == "ShareChange":
        return "#,##0"
    return None


def _write_section(
    xw: pd.ExcelWriter, sheet: str, title: str | None, df: pd.DataFrame, row: int
) -> int:
    """Write a table (with optional title line) at 0-based `row`; return next free row."""
    if title is None:
        df.to_excel(xw, sheet_name=sheet, startrow=row, index=False)
        ws = xw.sheets[sheet]
        header_excel_row = row + 1
    else:
        df.to_excel(xw, sheet_name=sheet, startrow=row + 1, index=False)
        ws = xw.sheets[sheet]
        ws.cell(row=row + 1, column=1, value=title).font = Font(bold=True, size=12)
        header_excel_row = row + 2

    if df.empty:
        ws.cell(row=header_excel_row, column=1, value="None")
        return row + 4

    first_data_row = header_excel_row + 1
    for col_idx, name in enumerate(df.columns, start=1):
        fmt = _number_format(name)
        if fmt:
            for r in range(first_data_row, first_data_row + len(df)):
                ws.cell(row=r, column=col_idx).number_format = fmt
    return row + len(df) + 4


def _autosize(ws) -> None:
    for column_cells in ws.columns:
        longest = max(
            (len(str(c.value)) for c in column_cells if c.value is not None), default=8
        )
        ws.column_dimensions[column_cells[0].column_letter].width = min(
            max(longest + 2, 10), 42
        )


def write_workbook(
    path: Path, table: pd.DataFrame, meta: dict, has_previous: bool
) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        # Tab 1: Holdings
        _write_section(xw, "Holdings", None, table, row=0)
        holdings_ws = xw.sheets["Holdings"]
        holdings_ws.freeze_panes = "A2"
        holdings_ws.auto_filter.ref = holdings_ws.dimensions

        # Tab 2: New & Closed
        if has_previous:
            new, closed = build_new_closed(table)
            nxt = _write_section(
                xw, "New & Closed", f"NEW positions ({len(new)})", new, 0
            )
            _write_section(
                xw, "New & Closed", f"CLOSED positions ({len(closed)})", closed, nxt
            )

        # Tab 3: Summary
        pd.DataFrame().to_excel(xw, sheet_name="Summary")
        ws = xw.sheets["Summary"]
        ws.cell(row=1, column=1, value="Summary").font = Font(bold=True, size=14)
        rows = summary_rows(table, meta, has_previous)
        for i, (label, value) in enumerate(rows, start=3):
            ws.cell(row=i, column=1, value=label).font = Font(bold=True)
            cell = ws.cell(row=i, column=2, value=value)
            if isinstance(value, float):
                cell.number_format = '0.0"%"' if "%" in label else "$#,##0"
        if has_previous:
            up, down = build_top_changes(table)
            nxt = 3 + len(rows) + 1  # 0-based row after the key/value block
            nxt = _write_section(
                xw, "Summary", f"Top {TOP_N} stock increases (est. trade size)", up, nxt
            )
            nxt = _write_section(
                xw,
                "Summary",
                f"Top {TOP_N} stock decreases (est. trade size)",
                down,
                nxt,
            )
            oup, odown = build_top_changes(table, options=True)
            if not oup.empty or not odown.empty:
                nxt = _write_section(
                    xw,
                    "Summary",
                    f"Top {TOP_N} OPTION increases (calls/puts)",
                    oup,
                    nxt,
                )
                _write_section(
                    xw,
                    "Summary",
                    f"Top {TOP_N} OPTION decreases (calls/puts)",
                    odown,
                    nxt,
                )

        for sheet in xw.sheets.values():
            _autosize(sheet)


# --------------------------------------------------------------------------- filers & state


def load_filers() -> list[dict]:
    return (
        json.loads(FILERS_FILE.read_text(encoding="utf-8"))
        if FILERS_FILE.exists()
        else []
    )


def load_state() -> dict:
    return (
        json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if STATE_FILE.exists()
        else {}
    )


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def cik_key(filer: str) -> str | None:
    """Normalize a numeric CIK to a stable key; tickers return None."""
    return str(int(filer)) if filer.isdigit() else None


def next_filing_window(period: str) -> tuple[date, date]:
    """Quarter-end of the next 13F period and its due date (45 days later)."""
    nxt = (pd.Timestamp(period) + pd.offsets.QuarterEnd(1)).date()
    return nxt, nxt + pd.Timedelta(days=FILING_DEADLINE_DAYS)


def tracker_status(entry: dict | None, today: date) -> tuple[str, str, str]:
    """(next period end, next due date, status) for the tracker."""
    if not entry:
        return "", "", "Not run yet"
    next_end, due = next_filing_window(entry["period"])
    if today > due:
        status = "Past due date - check now (may have filed)"
    elif today >= next_end:
        status = f"Filing window open - due in {(due - today).days} days"
    else:
        status = f"Up to date - window opens in {(next_end - today).days} days"
    return str(next_end), str(due), status


def write_tracker(state: dict) -> None:
    today = date.today()
    rows = []
    for f in load_filers():
        entry = state.get(cik_key(f["cik"]))
        next_end, due, status = tracker_status(entry, today)
        rows.append(
            {
                "Group": f.get("group", ""),
                "Name": f["name"],
                "Person": f.get("person", ""),
                "CIK": f["cik"],
                "Last period": entry["period"] if entry else "",
                "Last filing date": entry["filing_date"] if entry else "",
                "Next period": next_end,
                "Next due date": due,
                "Status": status,
            }
        )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with pd.ExcelWriter(TRACKER_FILE, engine="openpyxl") as xw:
            df = pd.DataFrame(rows)
            df.to_excel(xw, sheet_name="Tracker", index=False)
            ws = xw.sheets["Tracker"]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            _autosize(ws)
    except PermissionError:
        log.warning("Tracker.xlsx is open in Excel; close it to refresh the tracker.")


def check_listed_name(filer: str, company_name: str) -> None:
    """Warn if the CIK's SEC name shares no word with the name listed in filers.json."""
    listed = next(
        (f for f in load_filers() if cik_key(f["cik"]) == cik_key(filer)), None
    )
    if not listed:
        return
    words = lambda t: {w for w in re.findall(r"[a-z]{4,}", t.lower())}
    if not words(listed["name"]) & words(company_name):
        log.warning(
            "CIK check: listed as %r but SEC says %r. Verify the CIK in filers.json.",
            listed["name"],
            company_name,
        )


# --------------------------------------------------------------------------- run


def process(filer: str, quarters: int, rebuild: bool = False) -> list[Path]:
    from edgar import Company, set_identity  # imported late: see configure_environment

    set_identity(os.environ["EDGAR_IDENTITY"])

    company = Company(int(filer) if filer.isdigit() else filer)
    log.info("Filer: %s (CIK %s)", company.name, company.cik)
    check_listed_name(str(company.cik), company.name)

    filings = company.get_filings(form="13F-HR")
    if filings is None or len(filings) == 0:
        raise SystemExit(f"No 13F-HR filings found for {filer!r}.")

    latest = filings.latest(quarters)
    selected = (
        [latest] if quarters == 1 or not hasattr(latest, "__len__") else list(latest)
    )

    listed = next(
        (f for f in load_filers() if cik_key(f["cik"]) == cik_key(str(company.cik))),
        None,
    )
    folder = OUTPUT_DIR / safe_name(listed["name"] if listed else company.name)
    folder.mkdir(parents=True, exist_ok=True)

    state = load_state()
    key = str(int(company.cik))
    written: list[Path] = []

    for i, filing in enumerate(selected):
        period = str(filing.period_of_report)
        out = folder / f"{period}_13F.xlsx"

        if (
            i == 0
        ):  # newest filing: remember it for the tracker, even if already processed
            state[key] = {
                "name": company.name,
                "period": period,
                "filing_date": str(filing.filing_date),
                "accession": filing.accession_no,
                "file": str(out.relative_to(PROJECT_ROOT)),
            }

        if out.exists() and not rebuild:
            next_end, due = next_filing_window(period)
            log.info(
                "Already up to date: %s filed %s for period %s (%s). "
                "Next filing is for %s, due by %s. Nothing to do.",
                company.name,
                filing.filing_date,
                period,
                out.relative_to(PROJECT_ROOT),
                next_end,
                due,
            )
            continue

        report = filing.obj()
        if report is None or not report.has_infotable:
            log.warning("Skipping %s: no information table.", filing.accession_no)
            continue

        table, has_previous = build_table(report)
        meta = {
            "manager": company.name,
            "filing_date": filing.filing_date,
            "accession": filing.accession_no,
            "total_value": report.total_value,
        }
        try:
            write_workbook(out, table, meta, has_previous)
        except PermissionError:
            raise SystemExit(
                f"Can't write {out.name}. Close it in Excel and run again."
            ) from None
        written.append(out)

        log.info(
            "%s | period %s | %d rows | %s -> %s",
            filing.accession_no,
            period,
            len(table),
            "with changes vs last quarter" if has_previous else "no previous quarter",
            out.relative_to(PROJECT_ROOT),
        )

    save_state(state)
    write_tracker(state)
    return written


def run_all(rebuild: bool = False) -> int:
    """Run every filer in filers.json (latest quarter). Failures don't stop the batch."""
    filers = load_filers()
    new, skipped, failed = [], [], []
    for n, f in enumerate(filers, start=1):
        log.info("=== [%d/%d] %s ===", n, len(filers), f["name"])
        try:
            written = process(f["cik"], 1, rebuild)
            (new if written else skipped).append(f["name"])
        except SystemExit as exc:
            failed.append((f["name"], str(exc.code)))
            log.error("%s: %s", f["name"], exc.code)
        except Exception as exc:  # keep going: one bad filer shouldn't stop the rest
            failed.append((f["name"], f"{type(exc).__name__}: {exc}"))
            log.exception("%s failed", f["name"])

    log.info(
        "=== DONE: %d new, %d already up to date, %d failed ===",
        len(new),
        len(skipped),
        len(failed),
    )
    for name in new:
        log.info("  new: %s", name)
    for name, why in failed:
        log.info("  FAILED: %s -> %s", name, why)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export 13F-HR holdings to Excel.")
    parser.add_argument("--filer", help="Ticker or CIK, e.g. BRK-A or 1067983")
    parser.add_argument(
        "--all", action="store_true", help="Run every filer in filers.json"
    )
    parser.add_argument(
        "--quarters", type=int, default=1, help="Recent filings to process"
    )
    parser.add_argument(
        "--tracker-only", action="store_true", help="Refresh Tracker.xlsx, no downloads"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Re-make workbooks that already exist (use after the Excel layout changes)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.tracker_only:
        write_tracker(load_state())
        log.info("Tracker refreshed -> %s", TRACKER_FILE.relative_to(PROJECT_ROOT))
        return 0
    if not args.filer and not args.all:
        parser.error("--filer is required (or use --all / --tracker-only)")

    configure_environment()
    if args.all:
        return run_all(args.rebuild)
    process(args.filer, max(1, args.quarters), args.rebuild)
    return 0
