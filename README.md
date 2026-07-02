# POS Header Mapper

Upload any POS/distributor file with a messy or inconsistent header row, map its
columns to your standard schema once, and the mapping is remembered — by
normalized column name — for every future file. No more re-mapping the same
headers every time a distributor sends you a file.

## What it does

Built for the common case where one POS/commission workbook spreads your
data across several sheets (e.g. a "Detail" sheet for direct sales, a "POS"
sheet for distributor sales, and a "Summary" sheet you don't want) — and
where you get a new file like this every month/week.

1. Upload one or many `.csv`/`.xlsx`/`.xls` files at once.
2. **New sheet types** (by sheet name) get a one-time setup: include or skip
   it, its header row, and an **anchor column** — a column that's always
   filled on a real data row. This is what lets the tool automatically throw
   out subtotal rows, blank separator rows, and stray pivot tables that
   often sit below the real data in the same sheet, without you deleting
   them by hand. The tool guesses a sensible anchor (preferring a date
   column) and you can override it.
3. **New columns** in included sheets get mapped to your standard schema,
   same as before — 🟢 pre-filled from memory or an exact name match, 🟡 new,
   pick once.
4. Click **Save mapping & generate merged file** — every sheet decision and
   column mapping is written to disk (`sheet_profiles.json`, `mappings.json`)
   and reused automatically for every future upload with the same sheet
   names/headers. All included sheets from all uploaded files are combined
   into **one** output table.
5. Download the result as `.csv` or `.xlsx`.

The sidebar lets you review/forget individual remembered sheet setups and
column mappings, and edit your standard column list
(`standard_headers.json`) if your schema changes.

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
