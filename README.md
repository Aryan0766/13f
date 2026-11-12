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

- **13F: Berkshire (latest)** — quick test.
- **13F: Ask for ticker/CIK** — prompts for a ticker or CIK and how many quarters.

Results land in `data/output/*.csv` (Task: **Open output folder**).

## Reset / uninstall

Delete the project folder. Nothing else on Windows was touched.
