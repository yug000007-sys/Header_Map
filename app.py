"""
POS Header Mapper — multi-sheet, multi-file merge
---------------------------------------------------
Handles files where your data is split across several sheets (e.g. a
"Detail" sheet for direct sales and a "POS" sheet for distributor sales,
plus a "Summary" sheet you don't want). You set up each sheet type once
(include/exclude, header row, and an "anchor" column used to drop subtotal
/ pivot / blank rows) and map its columns once. Every future file with the
same sheet names and headers is processed automatically, and every sheet
from every file you upload is combined into a single output table.

Run locally:
    streamlit run app.py
"""

import json
import os
import re
from datetime import datetime
from io import BytesIO

import pandas as pd
import streamlit as st

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STD_HEADERS_FILE = os.path.join(APP_DIR, "standard_headers.json")
MAPPINGS_FILE = os.path.join(APP_DIR, "mappings.json")
SHEET_PROFILES_FILE = os.path.join(APP_DIR, "sheet_profiles.json")
IGNORE_LABEL = "— ignore this column —"

DEFAULT_STD_HEADERS = [
    "Distname", "Supplier_name", "direct_indirect", "in_out_territory", "CustAccNbr",
    "CustDunsID", "CustName", "Address1", "City", "State", "County", "Zip", "Phone",
    "Country", "NoOfEmployees", "WebAddress", "SIC", "NAICS", "LineOfBusiness",
    "ParentName", "AccountType", "UOM", "InvoiceNumber", "Qty", "UnitCost", "UnitResale",
    "InvoiceDate", "DateRecieved", "PartNumberSubmitted", "PartNumberDescription",
    "Branch", "SalesRep", "Latitude", "Longitude", "Brand", "PartNumberActual", "UPCCode",
    "rawcustname", "rawdistaddress", "rawdistcity", "rawdiststate", "rawdistpostalcode",
    "rawdistcountry", "currency", "contractID", "client_CustName", "Zip_4_digit",
    "dnb_trade_style", "dnb_sales_value", "google_CustName", "google_Address1",
    "google_State", "google_Zip", "google_Country", "google_Phone", "google_WebAddress",
    "Pay_Month", "Pay_Year", "Ship_Month", "Ship_Year", "Industry", "Commissions",
    "Commission_Rate", "Cust_AM", "CEM", "Sales", "In_Out", "Commission_split_percentage",
    "Distributor_part_number", "Category", "google_City", "Billings", "Cheque_Number",
    "Pay_Date", "meta_data_json", "SO_Number", "PO_Number", "ship_date", "searched_on_google",
]

ANCHOR_TYPES = ["date", "number", "text"]


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def normalize(s):
    s = str(s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------
def is_blank(v):
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() == ""


def detect_header_row(raw_df, max_scan=15):
    """First row (within the first max_scan rows) with >= 3 non-empty cells."""
    for i in range(min(max_scan, len(raw_df))):
        non_empty = sum(1 for v in raw_df.iloc[i] if not is_blank(v))
        if non_empty >= 3:
            return i + 1  # 1-indexed
    return 1


def get_headers_and_col_indices(raw_df, header_row):
    header_vals = raw_df.iloc[header_row - 1].tolist()
    keep_idx = [i for i, h in enumerate(header_vals) if not is_blank(h)]
    headers = [str(header_vals[i]).strip() for i in keep_idx]
    return headers, keep_idx


def looks_like_date(v):
    if is_blank(v):
        return False
    if isinstance(v, datetime):
        return True
    s = str(v).strip()
    # cheap pre-filter so plain numbers/short text don't get misparsed as dates
    if not re.search(r"\d", s) or len(s) < 6:
        return False
    try:
        pd.to_datetime(s)
        return True
    except (ValueError, TypeError):
        return False


def guess_anchor(raw_df, header_row, headers, keep_idx, sample=20):
    """Pick the column most likely to reliably identify a 'real' data row:
    prefer a column that's mostly real dates, otherwise the leftmost column
    that's mostly non-empty."""
    sample_rows = raw_df.iloc[header_row: header_row + sample]

    date_scores = []
    for orig_i in keep_idx:
        vals = sample_rows.iloc[:, orig_i] if orig_i < sample_rows.shape[1] else pd.Series(dtype=object)
        date_count = sum(1 for v in vals if looks_like_date(v))
        date_scores.append(date_count)
    if date_scores and max(date_scores) >= max(3, len(sample_rows) // 4):
        idx = date_scores.index(max(date_scores))
        return headers[idx], "date"

    # fallback: leftmost column with the highest non-empty ratio
    best_col, best_type, best_score = headers[0] if headers else None, "text", -1
    for local_i, orig_i in enumerate(keep_idx):
        vals = sample_rows.iloc[:, orig_i] if orig_i < sample_rows.shape[1] else pd.Series(dtype=object)
        non_empty = sum(1 for v in vals if not is_blank(v))
        if non_empty > best_score:
            best_score, best_col = non_empty, headers[local_i]
            numeric_ok = 0
            for v in vals:
                if is_blank(v):
                    continue
                try:
                    float(v)
                    numeric_ok += 1
                except (ValueError, TypeError):
                    pass
            best_type = "number" if numeric_ok >= max(1, non_empty * 0.8) else "text"
    return best_col, best_type


def row_is_valid(value, anchor_type):
    if is_blank(value):
        return False
    if anchor_type == "date":
        return looks_like_date(value)
    if anchor_type == "number":
        try:
            float(value)
            return True
        except (ValueError, TypeError):
            return False
    return True  # text: any non-empty value counts


def extract_valid_rows(raw_df, header_row, headers, keep_idx, anchor_col, anchor_type):
    """Return a DataFrame of only the rows that pass the anchor-column check,
    i.e. real data rows — not subtotal rows, pivot tables, or blank separators."""
    anchor_local_idx = headers.index(anchor_col) if anchor_col in headers else 0
    anchor_orig_idx = keep_idx[anchor_local_idx]

    body = raw_df.iloc[header_row:, keep_idx].copy()
    body.columns = headers
    anchor_series = raw_df.iloc[header_row:, anchor_orig_idx]
    mask = anchor_series.apply(lambda v: row_is_valid(v, anchor_type))
    return body[mask.values].reset_index(drop=True)


def read_sheets(uploaded_file):
    """Returns dict: sheet_name -> raw_df (header=None). For CSVs, a single
    pseudo-sheet named after the file."""
    name = uploaded_file.name
    if name.lower().endswith(".csv"):
        df = pd.read_csv(uploaded_file, header=None, dtype=str)
        return {re.sub(r"\.[^.]+$", "", name): df}
    xls = pd.ExcelFile(uploaded_file)
    out = {}
    for sheet in xls.sheet_names:
        out[sheet] = xls.parse(sheet, header=None, dtype=str)
    return out


# --------------------------------------------------------------------------
# app state
# --------------------------------------------------------------------------
st.set_page_config(page_title="POS Header Mapper", page_icon="🗂️", layout="wide")

if "std_headers" not in st.session_state:
    st.session_state.std_headers = load_json(STD_HEADERS_FILE, DEFAULT_STD_HEADERS)
    if not os.path.exists(STD_HEADERS_FILE):
        save_json(STD_HEADERS_FILE, st.session_state.std_headers)

if "mappings" not in st.session_state:
    st.session_state.mappings = load_json(MAPPINGS_FILE, {})

if "sheet_profiles" not in st.session_state:
    st.session_state.sheet_profiles = load_json(SHEET_PROFILES_FILE, {})

if "output_df" not in st.session_state:
    st.session_state.output_df = None


# --------------------------------------------------------------------------
# sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("Remembered sheet setups")
    st.caption(f"{len(st.session_state.sheet_profiles)} sheet type(s) remembered")
    for norm_name, prof in sorted(st.session_state.sheet_profiles.items()):
        c1, c2 = st.columns([5, 1])
        status = "included" if prof.get("include") else "skipped"
        c1.markdown(
            f"`{norm_name}` — **{status}**"
            + (f", header row {prof.get('header_row')}, anchor `{prof.get('anchor_column')}` ({prof.get('anchor_type')})"
               if prof.get("include") else "")
        )
        if c2.button("✕", key=f"delprof_{norm_name}", help="Forget this sheet setup"):
            del st.session_state.sheet_profiles[norm_name]
            save_json(SHEET_PROFILES_FILE, st.session_state.sheet_profiles)
            st.rerun()

    st.divider()
    st.header("Remembered column mappings")
    st.caption(f"{len(st.session_state.mappings)} header(s) remembered")
    if st.session_state.mappings:
        for norm_src in sorted(st.session_state.mappings.keys()):
            target = st.session_state.mappings[norm_src]
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"`{norm_src}` → **{target or '_(ignored)_'}**")
            if c2.button("✕", key=f"delmap_{norm_src}", help="Forget this mapping"):
                del st.session_state.mappings[norm_src]
                save_json(MAPPINGS_FILE, st.session_state.mappings)
                st.rerun()
    if st.button("Clear all remembered mappings & sheet setups"):
        st.session_state.mappings = {}
        st.session_state.sheet_profiles = {}
        save_json(MAPPINGS_FILE, {})
        save_json(SHEET_PROFILES_FILE, {})
        st.rerun()

    st.divider()
    st.header("Standard columns")
    st.caption("Your target schema. One column name per line.")
    std_text = st.text_area(
        "Standard columns", value="\n".join(st.session_state.std_headers),
        height=200, label_visibility="collapsed",
    )
    if st.button("Save standard columns"):
        new_list = [line.strip() for line in std_text.split("\n") if line.strip()]
        if new_list:
            st.session_state.std_headers = new_list
            save_json(STD_HEADERS_FILE, new_list)
            st.success("Saved.")
            st.rerun()
        else:
            st.error("Enter at least one column name.")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
st.title("🗂️ POS Header Mapper")
st.caption(
    "Upload one or many POS files, even ones where your data is spread across "
    "several sheets. Set up each sheet type and column mapping once — every "
    "future file with the same shape is merged automatically into one output."
)

uploaded_files = st.file_uploader(
    "Upload POS file(s)", type=["csv", "xlsx", "xls"], accept_multiple_files=True
)

if uploaded_files:
    # 1) Read every sheet of every file
    file_sheets = {}  # filename -> {sheet_name: raw_df}
    all_sheet_names = set()
    for f in uploaded_files:
        sheets = read_sheets(f)
        file_sheets[f.name] = sheets
        all_sheet_names.update(sheets.keys())

    # 2) Find sheet names with no saved profile yet
    unresolved = sorted({name for name in all_sheet_names if normalize(name) not in st.session_state.sheet_profiles})

    if unresolved:
        st.subheader("Step 1 — Set up new sheet type(s)")
        st.caption(
            "These sheet names haven't been configured yet. For each: choose whether "
            "it holds row-level data you want, its header row, and an anchor column "
            "(a column that's always filled on a real data row — used to automatically "
            "drop subtotal, pivot-table, and blank rows)."
        )
        widget_state = {}
        for sheet_name in unresolved:
            norm_name = normalize(sheet_name)
            # find one representative raw_df for this sheet name (from first file that has it)
            sample_df = next(
                sheets[sheet_name] for sheets in file_sheets.values() if sheet_name in sheets
            )
            with st.expander(f"Sheet: {sheet_name}", expanded=True):
                include = st.checkbox(
                    "Include this sheet's rows in the merge",
                    value=not any(k in normalize(sheet_name) for k in ["summary", "pivot", "index", "readme"]),
                    key=f"inc_{norm_name}",
                )
                if include:
                    default_hr = detect_header_row(sample_df)
                    header_row = st.number_input(
                        "Header row (1-indexed)", min_value=1,
                        max_value=max(1, len(sample_df)), value=min(default_hr, max(1, len(sample_df))),
                        key=f"hr_{norm_name}",
                    )
                    headers, keep_idx = get_headers_and_col_indices(sample_df, header_row)
                    if headers:
                        guess_col, guess_type = guess_anchor(sample_df, header_row, headers, keep_idx)
                        anchor_col = st.selectbox(
                            "Anchor column (must be filled on every real data row)",
                            headers, index=headers.index(guess_col) if guess_col in headers else 0,
                            key=f"anchor_{norm_name}",
                        )
                        anchor_type = st.selectbox(
                            "Anchor column type", ANCHOR_TYPES,
                            index=ANCHOR_TYPES.index(guess_type) if guess_type in ANCHOR_TYPES else 0,
                            key=f"anchortype_{norm_name}",
                        )
                        st.caption(f"Detected columns: {', '.join(headers)}")
                    else:
                        st.warning("No columns detected on that header row.")
                widget_state[norm_name] = sheet_name

        if st.button("Save sheet setup", type="primary"):
            for norm_name, sheet_name in widget_state.items():
                include = st.session_state.get(f"inc_{norm_name}", False)
                if include:
                    st.session_state.sheet_profiles[norm_name] = {
                        "display_name": sheet_name,
                        "include": True,
                        "header_row": st.session_state.get(f"hr_{norm_name}", 1),
                        "anchor_column": st.session_state.get(f"anchor_{norm_name}"),
                        "anchor_type": st.session_state.get(f"anchortype_{norm_name}", "text"),
                    }
                else:
                    st.session_state.sheet_profiles[norm_name] = {
                        "display_name": sheet_name, "include": False,
                    }
            save_json(SHEET_PROFILES_FILE, st.session_state.sheet_profiles)
            st.rerun()

    else:
        # 3) All sheets resolved — extract rows from every included sheet of every file
        extracted = []  # list of (filename, sheet_name, headers, df_rows)
        for fname, sheets in file_sheets.items():
            for sheet_name, raw_df in sheets.items():
                prof = st.session_state.sheet_profiles.get(normalize(sheet_name))
                if not prof or not prof.get("include"):
                    continue
                header_row = prof["header_row"]
                headers, keep_idx = get_headers_and_col_indices(raw_df, header_row)
                if not headers:
                    continue
                anchor_col = prof.get("anchor_column") or headers[0]
                anchor_type = prof.get("anchor_type", "text")
                if anchor_col not in headers:
                    anchor_col = headers[0]
                rows_df = extract_valid_rows(raw_df, header_row, headers, keep_idx, anchor_col, anchor_type)
                extracted.append((fname, sheet_name, headers, rows_df))

        total_rows = sum(len(r[3]) for r in extracted)
        st.subheader("Step 2 — Review column mapping")
        st.caption(
            f"{len(uploaded_files)} file(s), {len(extracted)} included sheet(s), "
            f"{total_rows} data row(s) detected after filtering out subtotals/blanks."
        )
        with st.expander("Row counts per sheet"):
            for fname, sheet_name, headers, rows_df in extracted:
                st.write(f"- **{fname}** / *{sheet_name}*: {len(rows_df)} rows")

        # group by sheet name (same-named sheets across files share one mapping section),
        # preserving the order sheets were first seen
        sheets_in_order = []
        seen_sheet_names = set()
        headers_by_sheet = {}
        for fname, sheet_name, headers, rows_df in extracted:
            norm_sheet = normalize(sheet_name)
            if norm_sheet not in seen_sheet_names:
                seen_sheet_names.add(norm_sheet)
                sheets_in_order.append((norm_sheet, sheet_name))
                headers_by_sheet[norm_sheet] = headers  # every header from the raw file, no dedup

        std_options = [IGNORE_LABEL] + st.session_state.std_headers
        mapping_choices = {}  # (norm_sheet, src) -> chosen target
        total_header_count = 0
        auto_count = 0

        for norm_sheet, sheet_name in sheets_in_order:
            headers = headers_by_sheet[norm_sheet]
            st.markdown(f"#### 📄 {sheet_name}")
            st.caption(f"{len(headers)} column(s) found in this sheet")
            hc1, hc2, hc3 = st.columns([3, 1, 4])
            hc1.markdown("**Column in this sheet**")
            hc3.markdown("**Maps to your standard column**")
            for src in headers:
                total_header_count += 1
                n = normalize(src)
                saved = st.session_state.mappings.get(n)
                if saved is not None:
                    default_val = saved if saved else IGNORE_LABEL
                    auto_count += 1
                else:
                    exact = next((s for s in st.session_state.std_headers if normalize(s) == n), None)
                    default_val = exact if exact else IGNORE_LABEL
                    if exact:
                        auto_count += 1
                idx = std_options.index(default_val) if default_val in std_options else 0
                c1, c2, c3 = st.columns([3, 1, 4])
                dot = "🟢" if idx != 0 else "🟡"
                c1.markdown(f"{dot} `{src}`")
                c2.markdown("→")
                choice = c3.selectbox(
                    f"map_{norm_sheet}_{src}", std_options, index=idx,
                    key=f"map_{norm_sheet}_{src}", label_visibility="collapsed",
                )
                mapping_choices[(norm_sheet, src)] = choice
            st.divider()

        if total_header_count:
            st.caption(
                f"🟢 auto-filled from memory or exact name match · 🟡 needs your input "
                f"({auto_count}/{total_header_count} pre-filled)"
            )

        col_a, col_b = st.columns([1, 1])
        with col_a:
            generate_clicked = st.button("Save mapping & generate merged file", type="primary")
        with col_b:
            if st.button("Reset sheet setup (start over)"):
                st.session_state.sheet_profiles = {}
                save_json(SHEET_PROFILES_FILE, {})
                st.rerun()

        if generate_clicked:
            for (norm_sheet, src), choice in mapping_choices.items():
                n = normalize(src)
                st.session_state.mappings[n] = "" if choice == IGNORE_LABEL else choice
            save_json(MAPPINGS_FILE, st.session_state.mappings)

            combined_frames = []
            for fname, sheet_name, headers, rows_df in extracted:
                norm_sheet = normalize(sheet_name)
                out_df = pd.DataFrame(index=range(len(rows_df)), columns=st.session_state.std_headers)
                for src in headers:
                    choice = mapping_choices.get((norm_sheet, src), IGNORE_LABEL)
                    if choice != IGNORE_LABEL and src in rows_df.columns:
                        out_df[choice] = rows_df[src].values
                out_df["_source_file"] = fname
                out_df["_source_sheet"] = sheet_name
                combined_frames.append(out_df)

            final_df = pd.concat(combined_frames, ignore_index=True) if combined_frames else pd.DataFrame(
                columns=st.session_state.std_headers
            )
            final_df = final_df.fillna("")
            st.session_state.output_df = final_df
            st.success(f"Merged. {len(final_df)} rows ready to download below.")


if st.session_state.output_df is not None:
    st.divider()
    st.subheader("Download merged result")
    df = st.session_state.output_df
    st.dataframe(df.head(30), use_container_width=True)

    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇ Download CSV", csv_bytes, file_name="Merged_POS.csv", mime="text/csv")

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Merged")
    st.download_button(
        "⬇ Download XLSX", buf.getvalue(), file_name="Merged_POS.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
