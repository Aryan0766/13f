"""13F Tracker as a web page that runs on YOUR computer (nothing is uploaded).

Start it with "13F: Open as website (Chrome tab)" in VS Code's Run and Debug panel, or:
    streamlit run src/thirteenf_processor/web.py --server.address localhost
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from datetime import date

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from thirteenf_processor import bulk, combine
from thirteenf_processor.live import LIVE_DIR
from thirteenf_processor.processor import (
    OUTPUT_DIR,
    PROJECT_ROOT,
    cik_key,
    load_filers,
    load_state,
    tracker_status,
)

ENV_FILE = PROJECT_ROOT / ".env"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

load_dotenv(ENV_FILE)
st.set_page_config(page_title="13F Tracker", page_icon=":bar_chart:", layout="wide")


# --------------------------------------------------------------------------- identity


def _identity() -> tuple[str, str]:
    """(name, email) from EDGAR_IDENTITY, or empty strings if not set / still the placeholder."""
    raw = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not raw or "example.com" in raw:
        return "", ""
    parts = raw.split()
    email = next((p for p in reversed(parts) if "@" in p), "")
    name = " ".join(p for p in parts if p != email)
    return name, email


def _save_identity(name: str, email: str) -> None:
    """Write EDGAR_IDENTITY to .env (other lines are kept) and use it right away."""
    value = f"{name.strip()} {email.strip()}"
    lines = (
        ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    )
    out, done = [], False
    for line in lines:
        if line.strip().startswith("EDGAR_IDENTITY"):
            if not done:
                out.append(f"EDGAR_IDENTITY={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"EDGAR_IDENTITY={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ["EDGAR_IDENTITY"] = value


def sidebar() -> bool:
    """Name + email box. Returns True if an identity is set."""
    st.sidebar.header("Who is using this")
    st.sidebar.caption(
        "The SEC asks every program that downloads from EDGAR to say who it is. "
        "It is saved only in the .env file on this computer."
    )
    name, email = _identity()
    with st.sidebar.form("identity"):
        new_name = st.text_input("Your name", value=name)
        new_email = st.text_input("Your email", value=email)
        if st.form_submit_button("Save"):
            if not new_name.strip() or not EMAIL.match(new_email.strip()):
                st.error("Enter your name and a real email address.")
            elif "example.com" in new_email:
                st.error("Use your real email, not the example one.")
            else:
                _save_identity(new_name, new_email)
                st.rerun()
    if name and email:
        st.sidebar.success(f"Saved: {name}")
        return True
    st.sidebar.warning(
        "Add your name and email to use the download buttons. "
        "Stock lookup, screener and the combined view work without it."
    )
    return False


# --------------------------------------------------------------------------- cached data


def _stamp() -> tuple:
    """Changes whenever the index or the live data changes, so the cache refreshes itself."""
    files = [bulk.INDEX_DIR / "holders.parquet", *LIVE_DIR.rglob("*.parquet")]
    return tuple((str(f), f.stat().st_mtime) for f in files if f.exists())


if not hasattr(
    bulk, "_index_only_loader"
):  # keep the real loader safe across page reruns
    bulk._index_only_loader = bulk._load_holders
_original_loader = bulk._index_only_loader


@st.cache_resource(
    show_spinner="Loading the holder index (first time takes a minute)..."
)
def _holders(stamp: tuple):
    return _original_loader()


bulk._load_holders = lambda: _holders(
    _stamp()
)  # lookup and screener share one loaded copy


def _xlsx(frames: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for sheet, frame in frames.items():
            frame.to_excel(xw, sheet_name=sheet[:31], index=False)
            ws = xw.sheets[sheet[:31]]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
    return buf.getvalue()


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --------------------------------------------------------------------------- tab: lookup


def tab_lookup() -> None:
    st.subheader("Stock lookup: how many 13F filers hold it")
    query = st.text_input("Ticker or 9-character CUSIP", placeholder="NVDA").strip()
    if not query:
        return
    try:
        d = bulk.lookup_data(query)
    except ValueError as exc:
        st.warning(str(exc))
        return
    except SystemExit as exc:
        st.error(str(exc))
        return

    table = d["table"]
    st.markdown(f"### {d['query']}  {d['issuer']}")
    st.caption(f"CUSIP {', '.join(d['cusips'])}")
    done = table.loc[d["complete"]]
    if not done.empty:
        last = done.iloc[-1]
        st.metric(
            f"13F filers holding it, {last['Quarter']} (latest complete quarter)",
            f"{int(last['Filers']):,}",
            None if pd.isna(last["Change"]) else int(last["Change"]),
        )
        if d["streak"] >= 2:
            st.caption(
                f"Filer count has gone {d['direction']} {d['streak']} quarters in a row."
            )
    incomplete = table.loc[[not c for c in d["complete"]], "Quarter"].tolist()
    if incomplete:
        st.error(
            f"DATA INCOMPLETE for {', '.join(incomplete)}: filings are still coming in (or are "
            "missing from your ZIPs), so its count is too low. Do not compare it with earlier "
            "quarters yet."
        )
    if d["live_periods"]:
        st.info(
            f"{', '.join(d['live_periods'])} came straight from EDGAR and is not yet confirmed "
            "by the SEC data set."
        )
    st.bar_chart(table.set_index("Quarter")["Filers"])
    st.dataframe(table, hide_index=True, width="stretch")
    st.download_button(
        "Download as Excel",
        _xlsx({"13F filers": table}),
        file_name=f"{d['query']}_filers.xlsx",
        mime=XLSX,
    )
    st.caption(
        "Counts are 13F filers (institutional managers: Fidelity counts once), not funds."
    )


# --------------------------------------------------------------------------- tab: screener


def tab_screener() -> None:
    st.subheader("Market screener: biggest holder gainers and losers")
    c1, c2 = st.columns(2)
    early = c1.checkbox(
        "Early look (newest quarter is not complete yet)",
        help="Compares only filers that have filed for both quarters.",
    )
    include_funds = c2.checkbox("Include ETFs, funds and SPACs")
    if not st.button("Run screener", type="primary"):
        return
    try:
        with st.spinner("Screening..."):
            d = bulk.screen_data(include_funds, early)
    except SystemExit as exc:
        st.error(str(exc))
        return
    if d["fell_back"]:
        st.info("The newest quarter is already complete, so this is the normal screen.")
    title = "EARLY LOOK" if d["early"] else "Holder-count screen"
    st.markdown(
        f"### {title}: {d['latest']} vs {d['prev']}  "
        f"({d['screened']:,} stocks screened, {d['hidden']:,} ETFs/funds/SPACs hidden)"
    )
    if d["early"]:
        st.warning(
            f"EARLY LOOK: only the {d['cohort_n']:,} filers that filed for both quarters are "
            f"counted ({d['scale']:.0%} of {d['prev']}'s filers). Early filers are not a random "
            "sample, and the list will change as more filings arrive."
        )
    if d["live_note"]:
        st.info(d["live_note"])
    g, lo, st3 = st.tabs(["Gainers", "Losers", f"Rising {bulk.STREAK_MIN}+ quarters"])
    for tab, key in ((g, "gainers"), (lo, "losers"), (st3, "streaks")):
        with tab:
            st.dataframe(d[key], hide_index=True, width="stretch")
    notes = pd.DataFrame({"Notes": d["notes"]})
    st.download_button(
        "Download as Excel",
        _xlsx(
            {
                "Gainers": d["gainers"],
                "Losers": d["losers"],
                f"Rising {bulk.STREAK_MIN}+ qtrs": d["streaks"],
                "Notes": notes,
            }
        ),
        file_name=f"holder_screen_{'early_' if d['early'] else ''}{d['latest']}.xlsx",
        mime=XLSX,
    )
    with st.expander("Notes"):
        for line in d["notes"]:
            st.write("- " + line)


# --------------------------------------------------------------------------- tab: my filers


def _tracker() -> pd.DataFrame:
    state, today, rows = load_state(), date.today(), []
    for f in load_filers():
        entry = state.get(cik_key(f["cik"]))
        _next_end, due, status = tracker_status(entry, today)
        rows.append(
            {
                "Name": f["name"],
                "Person": f.get("person", ""),
                "Last period": entry["period"] if entry else "",
                "Last filing date": entry["filing_date"] if entry else "",
                "Next due date": due,
                "Status": status,
            }
        )
    return pd.DataFrame(rows)


def _periods_on_disk() -> list[str]:
    found = {p.name[:10] for p in OUTPUT_DIR.glob("*/*_13F.xlsx")}
    return sorted(found, reverse=True)


@st.cache_data(show_spinner="Combining the filer workbooks...")
def _combined(period: str | None, stamp: float) -> dict:
    return combine.build_tables(period)


def tab_filers() -> None:
    st.subheader(f"My {len(load_filers())} filers")
    st.markdown("**Filing tracker**")
    st.dataframe(_tracker(), hide_index=True, width="stretch")

    st.markdown("**Combined view**")
    periods = _periods_on_disk()
    if not periods:
        st.info(
            "No filer workbooks yet. Use the 'Run updates' tab to run all filers first."
        )
        return
    choice = st.selectbox(
        "Quarter",
        ["latest (each filer's newest)", *periods],
        help="Pick one quarter to compare everyone on the same quarter.",
    )
    period = None if choice.startswith("latest") else choice
    stamp = max(
        (p.stat().st_mtime for p in OUTPUT_DIR.glob("*/*_13F.xlsx")), default=0.0
    )
    try:
        d = _combined(period, stamp)
    except SystemExit as exc:
        st.error(str(exc))
        return
    if len(d["periods"]) > 1:
        st.warning(
            f"Filers are on different quarters ({', '.join(d['periods'])}). Pick one quarter "
            "above to compare everyone on the same one."
        )
    if d["missing"]:
        st.caption("No workbook for: " + "; ".join(d["missing"]))
    summary, stocks, buys, sells = st.tabs(
        ["Filer summary", "By stock", "Top buys", "Top sells"]
    )
    with summary:
        st.dataframe(d["summary"], hide_index=True, width="stretch")
    with stocks:
        find = (
            st.text_input("Search ticker or company", key="find_stock").strip().lower()
        )
        frame = d["stocks"]
        if find:
            hit = (
                frame["Ticker"].astype(str).str.lower().str.contains(find, regex=False)
            )
            hit |= (
                frame["Issuer"].astype(str).str.lower().str.contains(find, regex=False)
            )
            frame = frame[hit]
        st.dataframe(frame, hide_index=True, width="stretch")
    with buys:
        st.dataframe(d["top_buys"], hide_index=True, width="stretch")
    with sells:
        st.dataframe(d["top_sells"], hide_index=True, width="stretch")
    with st.expander("Notes"):
        st.dataframe(d["notes"], hide_index=True, width="stretch")
    st.download_button(
        "Download combined Excel",
        _xlsx(
            {
                "Filer summary": d["summary"],
                "By stock": d["stocks"],
                "Top buys": d["top_buys"],
                "Top sells": d["top_sells"],
                "All holdings": d["all_holdings"],
                "Notes": d["notes"],
            }
        ),
        file_name=f"All_Filers_{period or 'latest'}.xlsx",
        mime=XLSX,
    )


# --------------------------------------------------------------------------- tab: run updates


def run_job(
    label: str, module: str, args: list[str], needs_identity: bool, has_identity: bool
):
    """Run one of the project's commands and show its output live, like the VS Code terminal."""
    if needs_identity and not has_identity:
        st.error("Add your name and email in the sidebar first (the SEC requires it).")
        return
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    cmd = [sys.executable, "-u", "-m", module, *args]
    st.write(f"**{label}** is running. Please wait; clicking another button stops it.")
    box, lines = st.empty(), []
    proc = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip())
            box.code("\n".join(lines[-300:]), language=None)
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
    st.cache_resource.clear()  # new data may have arrived
    st.cache_data.clear()
    if code == 0:
        st.success(f"{label}: finished.")
    else:
        st.error(
            f"{label}: finished with problems (exit code {code}). Read the last lines above; "
            "lines starting with FAILED say which filer and why."
        )


def tab_updates(has_identity: bool) -> None:
    st.subheader("Run updates")
    st.caption(
        "These do the same as the VS Code buttons. Output appears below as it happens. "
        "Keep this tab open until a run finishes: closing it or clicking another button can stop it."
    )
    filers = load_filers()
    jobs = st.container()

    a, b = st.columns(2)
    with a:
        st.markdown("**Download my filers**")
        run_all = st.button("Run ALL filers (newest quarter)")
        refresh = st.button("Refresh tracker only")
        pick = st.selectbox(
            "One filer",
            filers,
            format_func=lambda f: f["name"],
            index=None,
            placeholder="Choose a filer",
        )
        quarters = st.number_input("How many recent quarters", 1, 8, 1)
        run_one = st.button("Run this filer")
    with b:
        st.markdown("**Newest quarter from EDGAR (live)**")
        live_period = st.text_input(
            "Quarter end (blank = the newest)",
            placeholder="2026-09-30",
            key="live_period",
        ).strip()
        live_args = ["--period", live_period] if live_period else []
        live_fetch = st.button("Live fetch (new filings only)")
        live_check = st.button("Live vs SEC data set check")
        st.markdown("**SEC bulk data set**")
        build = st.button("Build bulk index (after adding a new ZIP)")

    with jobs:
        if run_all:
            run_job(
                "Run ALL filers", "thirteenf_processor", ["--all"], True, has_identity
            )
        elif refresh:
            run_job(
                "Refresh tracker",
                "thirteenf_processor",
                ["--tracker-only"],
                False,
                True,
            )
        elif run_one:
            if pick is None:
                st.error("Choose a filer first.")
            else:
                run_job(
                    pick["name"],
                    "thirteenf_processor",
                    ["--filer", pick["cik"], "--quarters", str(int(quarters))],
                    True,
                    has_identity,
                )
        elif live_fetch:
            run_job(
                "Live fetch",
                "thirteenf_processor.live",
                ["--fetch", *live_args],
                True,
                has_identity,
            )
        elif live_check:
            run_job(
                "Live vs SEC check",
                "thirteenf_processor.live",
                ["--compare", *live_args],
                True,
                has_identity,
            )
        elif build:
            run_job(
                "Build bulk index", "thirteenf_processor.bulk", ["--build"], False, True
            )


# --------------------------------------------------------------------------- page


def main() -> None:
    has_identity = sidebar()
    st.title("13F Tracker")
    st.caption(
        "Information only, not investment advice. 13F data is a quarter-end snapshot, "
        "filed up to 45 days later."
    )
    look, screen, mine, updates = st.tabs(
        ["Stock lookup", "Market screener", "My filers", "Run updates"]
    )
    with look:
        tab_lookup()
    with screen:
        tab_screener()
    with mine:
        tab_filers()
    with updates:
        tab_updates(has_identity)


main()
