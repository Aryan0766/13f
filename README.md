# 13F Tracker

Download and explore SEC Form 13F filings (what big institutional investors hold) on your own computer. Built on [edgartools](https://github.com/dgunning/edgartools), run from VS Code or as a local website in your browser. Nothing is uploaded anywhere.

**Information only, not investment advice.** A 13F is a snapshot taken at quarter end and filed up to 45 days later. It shows what institutions held, not what they hold now. Options are left out of the stock counts.

## What it does

- **Stock lookup:** how many 13F filers hold a stock, per quarter, with the change and who was added or dropped.
- **Market screener:** stocks that gained or lost the most holders, stocks rising 3+ quarters in a row, an early look while the newest quarter is still incomplete, and a By sector view.
- **My filers:** one Excel workbook per filer from `filers.json` (holdings, new and closed positions, summary), a filing tracker, and a combined view across all of them with top buys, top sells and sectors.
- **Live quarter:** newest-quarter data straight from EDGAR, before the SEC publishes its data set.



## One-time setup (all clicks, no commands)

1. **Install Python 3.11 or newer** from python.org.
2. **Open this folder in VS Code** (File > Open Folder). Click **Yes** to trust it and **Install** on the recommended-extensions prompt.
3. **Create the environment:** `Ctrl+Shift+P` > **Python: Create Environment** > **Venv** > pick Python 3.11+ > tick **requirements-dev.txt** > OK.
4. **Add your SEC identity:** copy `.env.example`, rename the copy to `.env`, and put your real name and email in `EDGAR_IDENTITY`. (If you use the website, you can enter them in its sidebar instead.) The SEC asks every program that downloads from EDGAR to identify itself.
5. Check the bottom-right interpreter says `.venv`. If not: `Ctrl+Shift+P` > **Python: Select Interpreter**.

## Start the website

Run **13F: Open as website (Chrome tab)** in Run and Debug (`Ctrl+Shift+D`). It opens `http://localhost:8501`. Stop it with the stop button.

Without VS Code: double-click `start_13f.bat` (Windows) or `start_13f.command` (Mac; run `chmod +x start_13f.command` once first). If you copy the `.bat` elsewhere, set the `PROJ` line at its top to your project folder.

The page keeps data in memory while it runs, so the first lookup is slower. Keep the tab open until a run finishes.

## Which button updates what

| I want to update | Use |
|---|---|
| My filers' workbooks and the combined view | **Run ALL filers** (add `--rebuild` or **Rebuild ALL workbooks** to re-make existing files) |
| Stock lookup and screener for the newest quarter | **Live fetch** |
| Stock lookup and screener for older quarters | Put the new SEC ZIP in `data/sec_bulk/`, then **Build bulk index** |
| Sector labels | **Build sector labels** (one time; it can take 30-60 minutes) |

Run ALL filers does not update the lookup or screener, and the reverse.

## Bulk index (stock lookup and screener)

1. Download the SEC Form 13F data set ZIPs (sec.gov > Form 13F Data Sets) for the last year and put them in `data/sec_bulk/` (no unzipping).
2. Run **13F: Build bulk index**. Redo it whenever you add a ZIP.
3. Use **Stock lookup** or the **Market screener**.

A quarter is "complete" when it has at least 90% of the biggest quarter's filers. Incomplete quarters are flagged DATA INCOMPLETE and no change is shown for them. Counts are 13F filers (firms such as Fidelity count once), not individual funds. ETFs and funds are hidden by a name guess; fund names include the fund title when the filing only names the trust.

## Live quarter and early look

**Live fetch** downloads the newest quarter from EDGAR (the first run takes a while; later runs fetch only new filings, and you can stop any time). Lookup and screener show it tagged "live". When the SEC publishes its ZIP, add it, rebuild the index, and run **Live vs SEC data set check**. From then on the SEC data is used.

**Early look** compares the newest, incomplete quarter with the one before using only filers that have filed for both. Early filers are not a random sample, so treat it as a preview.

## My filers

`filers.json` lists the filers (name, CIK, group, person). Each gets a workbook at `data/output/<Filer>/<period>_13F.xlsx` with Holdings, New & Closed and Summary. Stocks and options are kept apart in the summary, so a big options book does not distort the stock numbers. **Combine all filers** writes `data/output/All_Filers_<quarter>.xlsx` (Filer summary, By stock, Top buys, Top sells, By sector, All holdings, Notes). Pick one quarter to compare everyone on the same quarter.

## Sector view

13F has no sector data, so each ticker is labelled from SEC industry codes, grouped into about 11 broad sectors. Labels are coarse and some stocks stay Unclassified. **By sector** shows direction only (being added to or trimmed, no dollar amounts). The screener view covers all 13F filers; the My filers view covers only your list. They are never added together.

## Folders

- `src/thirteenf_processor/`: the code. `web.py` is the website.
- `filers.json`: your filer list. `.env`: your identity (not shared).
- `data/`: all downloads and outputs (not shared).

## Troubleshooting

- **"EDGAR_IDENTITY is not set":** add your real name and email to `.env`, or the website sidebar.
- **A filer shows "No 13F-HR filings found":** check its CIK in `filers.json`.
- **"Can't write ... Close it in Excel":** close the workbook and run again.
- **Launcher says streamlit is missing:** run the **Install requirements** task.
