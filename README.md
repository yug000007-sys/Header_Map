# POS Header Mapper

Upload any POS/distributor file with a messy or inconsistent header row, map its
columns to your standard schema once, and the mapping is remembered — by
normalized column name — for every future file. No more re-mapping the same
headers every time a distributor sends you a file.

## What it does

Built for the common case where your commission/POS data comes from several
sources every month — a multi-sheet workbook, a single spreadsheet, or even
a PDF commission statement — and you don't want to redo the setup each time.

1. Upload one or many `.csv`/`.xlsx`/`.xls`/`.pdf` files at once.
2. **New sheet/PDF types** get a one-time setup:
   - **Spreadsheet sheets**: include or skip, header row, and an **anchor
     column** — a column that's always filled on a real data row, so the
     tool can automatically drop subtotal rows, blank separators, and stray
     pivot tables sitting below the real data. It guesses a sensible anchor
     (preferring a date column); you can override it.
   - **PDF statements** (commission-statement style tables — Salesperson /
     Customer ID / Customer Name / Order / Invoice / Invoice Date / money
     columns): the tool parses the table directly from the PDF text and
     lets you set two rules — **drop highlighted/colored rows** (e.g. tariff
     or adjustment lines flagged with a background color) and **skip rows
     where a chosen column is zero or blank** (e.g. skip if Payment Due is
     0.00). The PDF is recognized by its title line, so the same vendor's
     PDF is auto-processed every month without re-setup.
3. **New columns** get mapped to your standard schema — 🟢 pre-filled from
   memory or an exact name match, 🟡 new, pick once.
4. Click **Save mapping & generate merged file** — every sheet/PDF decision
   and column mapping is written to disk (`sheet_profiles.json`,
   `mappings.json`) and reused automatically for every future upload with
   the same sheet names/PDF title/headers. Everything included, from every
   file you upload, is combined into **one** output table.
5. Download the result as `.csv` or `.xlsx` — dates are cleaned to
   `YYYY-MM-DD` (no time-of-day) and money/rate-looking columns are rounded
   to 2 decimals, based on the standard column's name.

The sidebar lets you review/forget individual remembered sheet setups and
column mappings, and edit your standard column list
(`standard_headers.json`) if your schema changes.

### A note on the PDF parser

It's built for the common "commission statement" table shape: a customer ID
like `C000095`, an order number like `SO00000573` or `R000090259`, an
invoice number, dates, and trailing money columns. If a PDF's layout is very
different, the parser may not find rows — in that case tell me and I can
adjust the pattern-matching for that layout.

## Run locally

```bash
git clone <this-repo-url>
cd pos-header-mapper
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
streamlit run app.py
```

Then open the URL Streamlit prints (usually `http://localhost:8501`).

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repo.
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub.
3. **New app** → pick this repo/branch → set main file to `app.py` → **Deploy**.

### A note on persistence

Mappings are stored in `mappings.json` in the app's own folder. This works
perfectly for local use and for a long-running Streamlit Cloud instance. If
the Cloud instance sleeps/restarts (e.g. after inactivity or a redeploy), the
filesystem resets to what's in the GitHub repo — so mappings made *after* the
last push won't survive a restart.

If you want mappings to survive restarts on Streamlit Cloud without manual
`git push`, the simplest options are:

- **Periodically commit `mappings.json` back to the repo** (it's tracked by
  git on purpose, unlike a typical `.gitignore`d data file).
- **Swap the storage backend** for something external — a GitHub Gist, a
  small database (e.g. Supabase/Postgres), or S3 — by replacing `load_json` /
  `save_json` in `app.py`. The rest of the app doesn't need to change.

For self-hosted deployments (your own server, Docker, etc.) the local
`mappings.json` file persists normally and this isn't a concern.

## Project structure

```
pos-header-mapper/
├── app.py                  # the Streamlit app
├── standard_headers.json   # your target schema (edit via the sidebar or directly)
├── mappings.json           # remembered header → standard-column mappings
├── sheet_profiles.json     # remembered per-sheet-name setup (include/skip, header row, anchor column)
├── requirements.txt
├── .streamlit/config.toml  # theme
└── .gitignore
```

## Customizing the standard schema

Edit `standard_headers.json` directly, or use the "Standard columns" panel in
the app sidebar (one column name per line, saved with a button click).
