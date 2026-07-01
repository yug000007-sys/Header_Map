"""
POS Header Mapper
------------------
Upload any POS/distributor file with a messy or inconsistent header row,
map its columns to your standard schema once, and the tool remembers that
mapping (by normalized column name) for every future upload -- across
different files, different distributors, forever.

Run locally:
    streamlit run app.py

Deploy:
    Push this folder to GitHub and deploy on Streamlit Community Cloud
    (share.streamlit.io) -> point it at app.py.
"""

import json
import os
import re
from io import BytesIO

import pandas as pd
import streamlit as st

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STD_HEADERS_FILE = os.path.join(APP_DIR, "standard_headers.json")
MAPPINGS_FILE = os.path.join(APP_DIR, "mappings.json")
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
def best_sheet_guess(sheet_names, xls):
    """Pick the sheet most likely to hold row-level data: the one whose
    detected header row yields the most columns * data rows, rather than
    just the sheet with the most non-empty cells (which favors dense
    summary/pivot sheets over the real transaction table)."""
    best_name, best_score = sheet_names[0], -1
    for name in sheet_names:
        df = xls.parse(name, header=None, dtype=str, nrows=200)
        if df.empty:
            continue
        hr = detect_header_row(df)
        headers, body = extract_headers_and_data(df, hr)
        score = len(headers) * len(body)
        if score > best_score:
            best_score, best_name = score, name
    return best_name


def detect_header_row(raw_df, max_scan=15):
    """First row (within the first max_scan rows) with >= 3 non-empty cells."""
    for i in range(min(max_scan, len(raw_df))):
        non_empty = raw_df.iloc[i].astype(str).str.strip().replace("nan", "").ne("").sum()
        if non_empty >= 3:
            return i + 1  # 1-indexed
    return 1


def extract_headers_and_data(raw_df, header_row):
    header_vals = raw_df.iloc[header_row - 1].tolist()
    keep_idx = [i for i, h in enumerate(header_vals) if str(h).strip() not in ("", "nan", "None")]
    source_headers = [str(header_vals[i]).strip() for i in keep_idx]

    body = raw_df.iloc[header_row:, keep_idx].copy()
    body.columns = source_headers
    body = body.fillna("")
    # drop fully blank rows
    non_blank_mask = body.apply(lambda r: any(str(v).strip() != "" for v in r), axis=1)
    body = body[non_blank_mask].reset_index(drop=True)
    return source_headers, body


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

if "output_df" not in st.session_state:
    st.session_state.output_df = None


# --------------------------------------------------------------------------
# sidebar: remembered mappings + standard schema editor
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("Remembered mappings")
    st.caption(f"{len(st.session_state.mappings)} header(s) remembered")

    if st.session_state.mappings:
        for norm_src in sorted(st.session_state.mappings.keys()):
            target = st.session_state.mappings[norm_src]
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"`{norm_src}` → **{target or '_(ignored)_'}**")
            if c2.button("✕", key=f"del_{norm_src}", help="Forget this mapping"):
                del st.session_state.mappings[norm_src]
                save_json(MAPPINGS_FILE, st.session_state.mappings)
                st.rerun()
    else:
        st.caption("Nothing remembered yet — map a file and it'll show up here.")

    if st.session_state.mappings and st.button("Clear all remembered mappings"):
        st.session_state.mappings = {}
        save_json(MAPPINGS_FILE, {})
        st.rerun()

    st.divider()
    st.header("Standard columns")
    st.caption("Your target schema. One column name per line.")
    std_text = st.text_area(
        "Standard columns",
        value="\n".join(st.session_state.std_headers),
        height=220,
        label_visibility="collapsed",
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
    "Upload any distributor POS file, map its columns to your standard schema once, "
    "and it's remembered for every future file — no more re-mapping headers by hand."
)

uploaded = st.file_uploader("Upload a POS file", type=["csv", "xlsx", "xls"])

if uploaded is not None:
    is_csv = uploaded.name.lower().endswith(".csv")

    if is_csv:
        raw_df = pd.read_csv(uploaded, header=None, dtype=str)
        sheet_name = None
    else:
        xls = pd.ExcelFile(uploaded)
        default_sheet = best_sheet_guess(xls.sheet_names, xls)
        sheet_name = st.selectbox(
            "Sheet", xls.sheet_names, index=xls.sheet_names.index(default_sheet)
        )
        raw_df = xls.parse(sheet_name, header=None, dtype=str)

    default_header_row = detect_header_row(raw_df)
    header_row = st.number_input(
        "Header row (1-indexed) — adjust if this file has title rows above the real header",
        min_value=1,
        max_value=max(1, len(raw_df)),
        value=min(default_header_row, max(1, len(raw_df))),
    )

    source_headers, data_body = extract_headers_and_data(raw_df, header_row)

    if not source_headers:
        st.warning("No header columns detected on that row. Try a different header row.")
    else:
        st.write(f"Detected **{len(source_headers)}** columns and **{len(data_body)}** data rows.")

        st.subheader("Column mapping")
        std_options = [IGNORE_LABEL] + st.session_state.std_headers
        mapping_choices = {}

        auto_count = 0
        header_c1, header_c2, header_c3 = st.columns([3, 1, 4])
        header_c1.markdown("**Column in this file**")
        header_c3.markdown("**Maps to your standard column**")

        for src in source_headers:
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
                f"map_{src}", std_options, index=idx, key=f"map_{src}", label_visibility="collapsed"
            )
            mapping_choices[src] = choice

        st.caption(
            f"🟢 auto-filled from memory or exact name match · 🟡 needs your input "
            f"({auto_count}/{len(source_headers)} pre-filled)"
        )

        if st.button("Save mapping & generate file", type="primary"):
            for src, choice in mapping_choices.items():
                n = normalize(src)
                st.session_state.mappings[n] = "" if choice == IGNORE_LABEL else choice
            save_json(MAPPINGS_FILE, st.session_state.mappings)

            out_df = pd.DataFrame(
                {"__idx__": range(len(data_body))}
            ).drop(columns="__idx__")
            out_df = out_df.reindex(columns=st.session_state.std_headers)
            out_df = out_df.reindex(range(len(data_body)))
            for src, choice in mapping_choices.items():
                if choice != IGNORE_LABEL:
                    out_df[choice] = data_body[src].values
            out_df = out_df.fillna("")

            st.session_state.output_df = out_df
            st.session_state.output_basename = re.sub(r"\.[^.]+$", "", uploaded.name)
            st.success(f"Mapping saved. {len(out_df)} rows ready to download below.")


if st.session_state.output_df is not None:
    st.divider()
    st.subheader("Download")
    df = st.session_state.output_df
    st.dataframe(df.head(20), use_container_width=True)

    base = st.session_state.get("output_basename", "output")

    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇ Download CSV", csv_bytes, file_name=f"Formatted_{base}.csv", mime="text/csv"
    )

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Formatted")
    st.download_button(
        "⬇ Download XLSX",
        buf.getvalue(),
        file_name=f"Formatted_{base}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
