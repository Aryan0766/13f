"""Combine the per-filer workbooks into one Excel file (all filers in filers.json).

Reads the Holdings tab of each filer's workbook in data/output/<Filer name>/<period>_13F.xlsx
(no downloads). By default it takes each filer's newest quarter; with --period it takes
that quarter for every filer that has it and lists the ones that don't.

Tabs: Filer summary, By stock (how many of your filers hold each stock and who),
Top buys, Top sells, All holdings, Notes.
"""

from __future__ import annotations

import argparse
import logging
import re

import pandas as pd

from .processor import (
    OUTPUT_DIR,
    PROJECT_ROOT,
    _autosize,
    load_filers,
    safe_name,
)

TOP_N = 50
FILE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})_13F\.xlsx$")

log = logging.getLogger("thirteenf")


def _find_file(name: str, period: str | None):
    folder = OUTPUT_DIR / safe_name(name)
    if not folder.exists():
        return None, None
    found = {}
    for path in folder.glob("*_13F.xlsx"):
        m = FILE_PATTERN.match(path.name)
        if m:
            found[m.group(1)] = path
    if not found:
        return None, None
    if period:
        return (period, found[period]) if period in found else (None, None)
    newest = max(found)
    return newest, found[newest]


def load_all(period: str | None) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """(all holdings with filer columns, one row per filer used, names with no file)."""
    frames, used, missing = [], [], []
    for f in load_filers():
        got, path = _find_file(f["name"], period)
        if path is None:
            missing.append(f["name"])
            continue
        df = pd.read_excel(path, sheet_name="Holdings")
        if df.empty:
            missing.append(f["name"])
            continue
        meta = {
            "Filer": f["name"],
            "Group": f.get("group", ""),
            "Person": f.get("person", ""),
        }
        frames.append(df.assign(**meta))
        used.append({**meta, "Period": got, "File": path.name})
    if not frames:
        raise SystemExit(
            "No filer workbooks found in data/output. Run '13F: Run ALL filers' first"
            + (f" (and make sure they have a {period} quarter)." if period else ".")
        )
    return pd.concat(frames, ignore_index=True), pd.DataFrame(used), missing


def _prep(table: pd.DataFrame) -> pd.DataFrame:
    t = table.copy()
    for col in ("Ticker", "Issuer", "Cusip", "PutCall", "Status", "PrevPeriod"):
        if col not in t.columns:
            t[col] = ""
        t[col] = t[col].fillna("").astype(str)
    for col in ("Shares", "Value", "PrevShares", "PrevValue"):
        if col not in t.columns:
            t[col] = float("nan")
    t["Held"] = t["Shares"].fillna(0) > 0
    return t


def filer_summary(t: pd.DataFrame, used: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for u in used.to_dict("records"):
        f = t[t["Filer"] == u["Filer"]]
        held = f[f["Held"]]
        status = f["Status"].value_counts()
        total = held["Value"].sum()
        top = held.sort_values("Value", ascending=False).head(1)
        top_name = ""
        top_pct = None
        if not top.empty:
            r = top.iloc[0]
            top_name = r["Ticker"] or r["Issuer"]
            top_pct = r["Value"] / total * 100 if total else None
        rows.append(
            {
                "Filer": u["Filer"],
                "Group": u["Group"],
                "Person": u["Person"],
                "Period": u["Period"],
                "Compared with": f["PrevPeriod"].replace("", pd.NA).dropna().iloc[0]
                if f["PrevPeriod"].replace("", pd.NA).notna().any()
                else "",
                "Positions": len(held),
                "Total value": total,
                "New": int(status.get("NEW", 0)),
                "Closed": int(status.get("CLOSED", 0)),
                "Increased": int(status.get("INCREASED", 0)),
                "Decreased": int(status.get("DECREASED", 0)),
                "Biggest holding": top_name,
                "Biggest holding %": top_pct,
            }
        )
    return pd.DataFrame(rows)


def by_stock(t: pd.DataFrame) -> pd.DataFrame:
    """One row per stock (options excluded): how many filers hold it and what they did."""
    s = t[t["PutCall"] == ""].copy()
    s["key"] = s["Cusip"].where(s["Cusip"] != "", s["Issuer"])
    names = (
        s.sort_values("Value", ascending=False)
        .drop_duplicates("key")
        .set_index("key")[["Ticker", "Issuer"]]
    )
    holders = s[s["Held"]].groupby("key")
    out = pd.DataFrame(
        {
            "Filers holding": holders["Filer"].nunique(),
            "Total value": holders["Value"].sum(),
            "Holders": holders["Filer"].agg(lambda x: "; ".join(sorted(set(x)))),
        }
    )
    for label, status in (
        ("Opened (new)", "NEW"),
        ("Added to", "INCREASED"),
        ("Trimmed", "DECREASED"),
        ("Sold out", "CLOSED"),
    ):
        out[label] = s[s["Status"] == status].groupby("key")["Filer"].nunique()
    out = out.join(names, how="outer")
    out = out[out["Filers holding"].notna() | out["Sold out"].notna()]
    count_cols = ["Filers holding", "Opened (new)", "Added to", "Trimmed", "Sold out"]
    out[count_cols] = out[count_cols].fillna(0).astype(int)
    out["Total value"] = out["Total value"].fillna(0)
    out["Holders"] = out["Holders"].fillna("")
    out["Net buyers"] = (
        out["Opened (new)"] + out["Added to"] - out["Trimmed"] - out["Sold out"]
    )
    cols = [
        "Ticker",
        "Issuer",
        "Filers holding",
        "Net buyers",
        "Opened (new)",
        "Added to",
        "Trimmed",
        "Sold out",
        "Total value",
        "Holders",
    ]
    out = out.reset_index(names="Key")
    return out[["Key", *cols]].sort_values(
        ["Filers holding", "Total value"], ascending=False
    )


def _tab(xw: pd.ExcelWriter, name: str, df: pd.DataFrame) -> None:
    df.to_excel(xw, sheet_name=name, index=False)
    ws = xw.sheets[name]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    _autosize(ws)


def combine(period: str | None = None) -> int:
    table, used, missing = load_all(period)
    t = _prep(table)
    periods = sorted(set(used["Period"]))
    summary = filer_summary(t, used)
    stocks = by_stock(t).drop(columns=["Key"])
    # Buying/selling lists only make sense for filers that have a previous quarter to compare with
    moves = stocks[
        (
            stocks["Opened (new)"]
            + stocks["Added to"]
            + stocks["Trimmed"]
            + stocks["Sold out"]
        )
        > 0
    ]
    top_buys = moves.sort_values(
        ["Net buyers", "Filers holding"], ascending=False
    ).head(TOP_N)
    top_sells = moves.sort_values(["Net buyers", "Filers holding"]).head(TOP_N)

    notes = [
        ("Filers included", f"{len(used)} of {len(load_filers())}"),
        ("Quarters in this file", ", ".join(periods)),
    ]
    if len(periods) > 1:
        notes.append(
            (
                "WARNING",
                (
                    "Filers are on different quarters. Run again with a period "
                    "(e.g. 2026-06-30) to compare everyone on the same quarter."
                ),
            )
        )
    if missing:
        notes.append(("No workbook found for", "; ".join(missing)))
    notes += [
        (
            "Filers holding",
            "Filers (of yours) with a positive share count in this stock. Options are not counted.",
        ),
        (
            "Opened / Added to / Trimmed / Sold out",
            "How many of your filers did that versus their own previous quarter.",
        ),
        ("Net buyers", "Opened + Added to - Trimmed - Sold out."),
        (
            "Total value",
            "Sum of the reported position values of the filers holding it (dollars).",
        ),
        (
            "Not investment advice",
            "13F data is a quarter-end snapshot, filed up to 45 days later.",
        ),
    ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    label = period or ("Combined" if len(periods) > 1 else periods[0])
    out = OUTPUT_DIR / f"All_Filers_{label}.xlsx"
    try:
        with pd.ExcelWriter(out, engine="openpyxl") as xw:
            _tab(xw, "Filer summary", summary)
            _tab(xw, "By stock", stocks)
            _tab(xw, "Top buys", top_buys)
            _tab(xw, "Top sells", top_sells)
            cols = [
                c
                for c in ["Filer", "Group", "Person", *table.columns]
                if c in t.columns
            ]
            _tab(xw, "All holdings", t[list(dict.fromkeys(cols))])
            pd.DataFrame(notes, columns=["Item", "Note"]).to_excel(
                xw, sheet_name="Notes", index=False
            )
            _autosize(xw.sheets["Notes"])
    except PermissionError:
        raise SystemExit(
            f"Can't write {out.name}. Close it in Excel and run again."
        ) from None

    print(f"Combined {len(used)} filers ({', '.join(periods)}).")
    if len(periods) > 1:
        print("WARNING: filers are on different quarters; see the Notes tab.")
    if missing:
        print(f"No workbook for: {'; '.join(missing)}")
    print(f"Saved: {out.relative_to(PROJECT_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Combine all filer workbooks into one Excel."
    )
    parser.add_argument(
        "--period", help="Quarter end such as 2026-06-30 (default: each filer's newest)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    period = args.period.strip() if args.period else None
    if period and period.lower() in {"latest", "newest", ""}:
        period = None
    return combine(period)


if __name__ == "__main__":
    raise SystemExit(main())
