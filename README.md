# 13F Processor (edgartools) — VS Code, zero terminal typing

Everything lives inside this folder. Nothing is installed globally; the cache is in `data/edgar_cache`.

## One-time setup (all clicks, no commands)

1. **Install Python 3.11 or newer** (edgartools requires >=3.11). Use the python.org installer or
   Microsoft Store; no PATH changes needed beyond the default.
2. **Open this folder:** VS Code > File > Open Folder. Click **Yes** to trust it, and **Install** on the
   "recommended extensions" prompt (Python, Pylance, Ruff).
3. **Create the isolated environment:** `Ctrl+Shift+P` > **Python: Create Environment** > **Venv** >
   pick Python 3.11+ > tick **requirements-dev.txt** > OK. VS Code builds `.venv` here and installs
   edgartools for you (watch the progress in the bottom-right).
4. **Set your SEC identity:** right-click `.env.example` > Copy, paste in the same folder, rename the copy
   to `.env`, and put your real name and email in `EDGAR_IDENTITY`.
5. Confirm the bottom-right interpreter says `.venv`. If not: `Ctrl+Shift+P` > **Python: Select
   Interpreter** > `.venv`.

## Run

Open **Run and Debug** (`Ctrl+Shift+D`), pick a launch config, press the green play button:

- **13F: Pick filer** — dropdown of the 30 filers in `filers.json`. Skips with a message if the latest quarter is already saved.
- **13F: Other ticker or CIK** — any filer not in the list.
- **13F: Refresh tracker only** — rebuilds `data/output/Tracker.xlsx` (last filing date, next due date, status) with no downloads.

Output: `data/output/<Filer>/<period>_13F.xlsx`. Progress is remembered in `data/state.json`.

Results land in `data/output/*.xlsx` (Task: **Open output folder**). Each workbook has three tabs: **Holdings** (with changes vs last quarter), **New & Closed**, and **Summary**. Close the file in Excel before re-running.

After pulling in new requirements, use Terminal > Run Task > **Install requirements**.

## Reset / uninstall

Delete the project folder. Nothing else on Windows was touched.

## Bulk lookup: how many funds hold a stock

1. Download the SEC Form 13F data set ZIPs (sec.gov > Form 13F Data Sets) for the last year (4 files) and put them in `data/sec_bulk/` (no unzipping).
2. Run **13F: Build bulk index** (about 10 seconds per ZIP). Redo it whenever you add a new ZIP.
3. Run **13F: Stock lookup (funds holding)** and type a ticker such as NVDA.

Shows funds per quarter, change, funds added/dropped, and how many quarters in a row the count has moved. Quarters with missing filings are marked partial.

## Market screener: which stocks gained the most holders

Run **13F: Market screener (biggest holder gainers)**. It compares the latest complete quarter with the one before it and saves `data/output/Screens/holder_screen_<quarter>.xlsx` with tabs for top gainers, top losers, stocks rising 3+ quarters in a row, and notes. It needs at least two complete quarters in the index. ETFs, funds and SPACs are hidden by a name guess. Thresholds are the constants at the top of `bulk.py` (`MIN_HOLDERS`, `MIN_PREV`, `TOP_N`, `STREAK_MIN`).

## Live quarter: use filings before the SEC data set is out

1. Run **13F: Live fetch (newest quarter from EDGAR)** now and then (use Ctrl+F5, "Run Without Debugging"). The first run downloads every filing so far (roughly 20-40 minutes); later runs only fetch new ones. You can stop it any time; progress is saved.
2. **Stock lookup** and the **market screener** pick up the live quarter automatically (marked "live"). The screener only compares a quarter once it has at least 90% of the previous quarter's filers.
3. When the SEC data set for that quarter is out: put the ZIP in `data/sec_bulk/`, run **13F: Build bulk index**, then **13F: Live vs SEC data set check**. It writes `data/output/CrossChecks/live_vs_dataset_<quarter>.xlsx`. From then on the data set is used and the live files are ignored.

**Early look.** While the newest quarter is still incomplete, run **13F: Market screener EARLY look**. It compares the newest quarter with the one before using only the filers that have already filed for both, so the counts are like for like. Early filers are not a random sample, so treat it as a preview. Once the newest quarter reaches 90% of the biggest quarter, the normal screener uses it automatically.

## One Excel for all filers

**13F: Combine all filers (one Excel)** reads the workbooks you already have in `data/output/` and writes `data/output/All_Filers_<quarter>.xlsx` with tabs: Filer summary, By stock (how many of your filers hold each stock and who), Top buys, Top sells, All holdings and Notes. Type `latest` to use each filer's newest quarter, or a quarter end such as `2026-06-30` to compare everyone on the same quarter.