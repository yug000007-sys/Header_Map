# POS Header Mapper

Upload any POS/distributor file with a messy or inconsistent header row, map its
columns to your standard schema once, and the mapping is remembered — by
normalized column name — for every future file. No more re-mapping the same
headers every time a distributor sends you a file.

## What it does

1. Upload a `.csv`, `.xlsx`, or `.xls` file.
2. It picks the most likely sheet and header row automatically (both are
   adjustable, for files with title rows above the real header).
3. Each column in the file is matched against your standard schema:
   - 🟢 already known (from a saved mapping, or an exact name match) — pre-filled
   - 🟡 new — pick where it goes, once
4. Click **Save mapping & generate file** — your choices are written to
   `mappings.json` and applied automatically to every future file with that
   same header.
5. Download the result as `.csv` or `.xlsx`, already reshaped into your
   standard column order.

The sidebar lets you review or forget individual remembered mappings, and edit
your standard column list (`standard_headers.json`) if your schema changes.

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
├── requirements.txt
├── .streamlit/config.toml  # theme
└── .gitignore
```

## Customizing the standard schema

Edit `standard_headers.json` directly, or use the "Standard columns" panel in
the app sidebar (one column name per line, saved with a button click).
