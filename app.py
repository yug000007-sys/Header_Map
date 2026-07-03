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
import pdfplumber
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
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except OSError as e:
        st.error(
            f"⚠️ Could not save to `{os.path.basename(path)}` — your mapping/setup won't be "
            f"remembered next time. This usually means the app's folder is read-only in this "
            f"deployment (common on some hosting setups). Error: {e}"
        )
        return False


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


DATE_PATTERN_RE = re.compile(
    r"^\d{1,4}[/-]\d{1,2}[/-]\d{1,4}([ T]\d{1,2}:\d{2}(:\d{2})?)?$"
)


def looks_like_date(v):
    if is_blank(v):
        return False
    if isinstance(v, datetime):
        return True
    s = str(v).strip()
    if not DATE_PATTERN_RE.match(s):
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


DATE_COLUMN_KEYWORDS = ["date"]
MONEY_COLUMN_KEYWORDS = [
    "cost", "price", "amt", "amount", "commission", "sales", "bill",
    "resale", "value", "split", "percentage", "rate",
]


def clean_date_value(value):
    if is_blank(value):
        return ""
    try:
        return pd.to_datetime(value).strftime("%m/%d/%Y")
    except (ValueError, TypeError):
        return value


def clean_money_value(value):
    if is_blank(value):
        return ""
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (ValueError, TypeError):
        return value


def format_output_df(df, std_headers):
    """Strip the time-of-day from date-like columns and round money/rate-like
    columns to 2 decimals, based on the standard column's name — fixes
    '2026-04-30 00:00:00' -> '2026-04-30' and '7.8546260000000006' -> 7.85."""
    for col in std_headers:
        if col not in df.columns:
            continue
        name_lower = col.lower()
        if any(k in name_lower for k in DATE_COLUMN_KEYWORDS):
            df[col] = df[col].apply(clean_date_value)
        elif any(k in name_lower for k in MONEY_COLUMN_KEYWORDS):
            df[col] = df[col].apply(clean_money_value)
    return df


def read_sheets(uploaded_file):
    """Returns dict: sheet_name -> {"kind": "excel"/"pdf", ...}.
    Excel/CSV entries carry a raw 2D grid (raw_df). PDF entries carry
    already-parsed headers + rows_df (see extract_pdf_sheet)."""
    name = uploaded_file.name
    if name.lower().endswith(".pdf"):
        sheet_name, headers, rows_df, supports_highlight = extract_pdf_sheet(uploaded_file)
        return {sheet_name: {
            "kind": "pdf", "headers": headers, "rows_df": rows_df,
            "supports_highlight": supports_highlight,
        }}
    if name.lower().endswith(".csv"):
        df = pd.read_csv(uploaded_file, header=None, dtype=str)
        return {re.sub(r"\.[^.]+$", "", name): {"kind": "excel", "raw_df": df}}
    xls = pd.ExcelFile(uploaded_file)
    out = {}
    for sheet in xls.sheet_names:
        out[sheet] = {"kind": "excel", "raw_df": xls.parse(sheet, header=None, dtype=str)}
    return out


# --------------------------------------------------------------------------
# PDF ingestion (pattern-based, for commission-statement style tables:
# Salesperson | Customer ID | Ship-to + Customer Name | Order | Invoice |
# Invoice Date | Due Date | ... money columns ... )
# --------------------------------------------------------------------------
PDF_CUSTOMER_ID_RE = re.compile(r"^[A-Z]\d{5,8}$")
PDF_ORDER_TOKEN_RE = re.compile(r"^(SO|R)\d+$")
PDF_DATE_TOKEN_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}")
PDF_MONEY_TOKEN_RE = re.compile(r"-?[\d,]+\.\d{2}")
PDF_HEADERS = ["Customer", "CustomerName", "Order", "Invoice", "InvoiceDate", "CommissionBase", "PaymentDue"]


def cluster_words_into_rows(words, y_tol=2.5):
    rows = []
    current = []
    last_top = None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if last_top is None or abs(w["top"] - last_top) <= y_tol:
            current.append(w)
        else:
            rows.append(current)
            current = [w]
        last_top = w["top"]
    if current:
        rows.append(current)
    return rows


def pdf_row_is_highlighted(row_words, rects):
    """A row counts as highlighted if a non-white, non-dark-header-style
    filled rectangle overlaps most of its vertical span (pastel highlight
    colors used to flag adjustment/tariff rows in these statements)."""
    tops = [w["top"] for w in row_words]
    bots = [w["bottom"] for w in row_words]
    r_top, r_bot = min(tops), max(bots)
    for rect in rects:
        color = rect.get("non_stroking_color")
        if not rect.get("fill") or not color:
            continue
        if isinstance(color, (int, float)):
            r, g, b = color, color, color
        elif len(color) == 1:
            r = g = b = color[0]
        else:
            r, g, b = color[0], color[1], color[2]
        brightness = (r + g + b) / 3
        if brightness > 0.98 or brightness < 0.5:
            continue  # skip pure white and dark header bars
        overlap = min(r_bot, rect["bottom"]) - max(r_top, rect["top"])
        if overlap > 0.5 * (r_bot - r_top):
            return True
    return False


def parse_pdf_data_row(row_words):
    tokens = [w["text"] for w in sorted(row_words, key=lambda w: w["x0"])]
    text = " ".join(tokens)
    cust = next((t for t in tokens if PDF_CUSTOMER_ID_RE.match(t)), None)
    order = next((t for t in tokens if PDF_ORDER_TOKEN_RE.match(t)), None)
    if not cust or not order:
        return None
    ci, oi = tokens.index(cust), tokens.index(order)
    name_blob = " ".join(tokens[ci + 1: oi])
    m = re.match(r"^(\d+)?\s*(.*)$", name_blob)
    cust_name = m.group(2).strip() if m else name_blob

    invoice_num = next((t for t in tokens[oi + 1:] if t.isdigit()), None)
    inv_date = None
    if invoice_num:
        idx2 = tokens.index(invoice_num)
        for t in tokens[idx2 + 1:]:
            dm = PDF_DATE_TOKEN_RE.match(t)
            if dm:
                inv_date = dm.group(0)
                break

    money_tokens = PDF_MONEY_TOKEN_RE.findall(text)
    commission_base = money_tokens[1] if len(money_tokens) >= 7 else (money_tokens[0] if money_tokens else None)
    payment_due = money_tokens[-1] if money_tokens else None

    return {
        "Customer": cust, "CustomerName": cust_name, "Order": order,
        "Invoice": invoice_num, "InvoiceDate": inv_date,
        "CommissionBase": commission_base, "PaymentDue": payment_due,
    }


def extract_pdf_sheet_hhp(uploaded_file):
    """Format A: commission-statement style tables — Salesperson | Customer ID |
    Ship-to + Customer Name | Order | Invoice | Invoice Date | Due Date | ...
    money columns. Parses every page into a clean rows_df (columns =
    PDF_HEADERS + '_highlighted'). The pseudo 'sheet name' is derived from
    the document's title line so the same vendor's PDF is recognized
    automatically every month."""
    records = []
    title = None
    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue
            if title is None:
                lines = [l for l in (page.extract_text() or "").split("\n") if l.strip()]
                title = lines[0] if lines else uploaded_file.name
            rects = page.rects
            for row_words in cluster_words_into_rows(words):
                parsed = parse_pdf_data_row(row_words)
                if parsed:
                    parsed["_highlighted"] = pdf_row_is_highlighted(row_words, rects)
                    records.append(parsed)

    sheet_name = title or uploaded_file.name
    rows_df = pd.DataFrame(records, columns=PDF_HEADERS + ["_highlighted"])
    return sheet_name, PDF_HEADERS, rows_df, True  # supports_highlight


# --------------------------------------------------------------------------
# PDF ingestion, format B: hierarchical "Agent Commission Recap" reports —
# a customer header line (Customer# + Name/Address, no delimiter between
# them), followed by one or more invoice lines (Invoice# / Date / Customer
# PO# / Release# / Prod Line & Description / Sales Amt / Rate% / Earned),
# each possibly followed by continuation lines that repeat only the
# product-line + amount columns for the same invoice.
# --------------------------------------------------------------------------
AGENT_CUSTOMER_NUM_RE = re.compile(r"^\d{6,7}(-\d{1,4})?$")
AGENT_INVOICE_NUM_RE = re.compile(r"^\d{7,9}$")
AGENT_MONTHS = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
AGENT_PRODCODE_RE = re.compile(r"^\d{2,4}[A-Z]{0,2}$")
AGENT_BOILERPLATE_MARKERS = [
    "Report:", "Company", "Division:", "Agent :", "Customer#", "Invoice#",
    "COMMISSIONS REPORT", "Agent Recap", "Total of all",
]
AGENT_RECAP_HEADERS = [
    "CustomerNumber", "CustomerName", "Invoice", "InvoiceDate", "CustomerPO",
    "ProdLineCode", "ProdLineDescription", "SalesAmt", "RatePct", "Earned",
]


def split_name_from_address_blob(blob):
    """Customer name and address are glued together with no delimiter in
    these reports (e.g. 'GRAYBAR ELECTRIC CO. INC. #1072810 NORTH FIRST
    AVE...'). The address portion always starts at the first digit."""
    m = re.search(r"\d", blob)
    if not m:
        return blob.strip(), ""
    return blob[:m.start()].strip(), blob[m.start():].strip()


def detect_pdf_format(uploaded_file):
    with pdfplumber.open(uploaded_file) as pdf:
        text = pdf.pages[0].extract_text() or ""
    if "MONTHLY COMMISSIONS REPORT" in text or ("Agent :" in text and "Supplier:" in text):
        return "agent_recap"
    if "Salesperson" in text and ("Commission Base" in text or "Ship To" in text):
        return "hhp"
    return "hhp"  # fall back to the simpler flat-table parser


def extract_pdf_sheet_agent_recap(uploaded_file):
    records = []
    cur_cust_num = cur_cust_name = None
    cur_inv_num = cur_inv_date = None
    in_recap_section = False
    company, agent = None, None

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            for row_words in cluster_words_into_rows(words):
                tokens = [w["text"] for w in sorted(row_words, key=lambda w: w["x0"])]
                if not tokens:
                    continue
                line_text = " ".join(tokens)

                if company is None and line_text.startswith("Company"):
                    company = line_text.split(":", 1)[-1].strip()
                if agent is None and "Agent :" in line_text:
                    agent = line_text.split("Agent :", 1)[-1].split("Supplier:")[0].strip()

                if "Agent Recap" in line_text:
                    in_recap_section = True
                if in_recap_section:
                    continue
                if tokens[0].startswith("**"):
                    continue
                if any(m in line_text for m in AGENT_BOILERPLATE_MARKERS):
                    continue
                if set(tokens[0]) == {"-"}:
                    continue

                if AGENT_CUSTOMER_NUM_RE.match(tokens[0]) and not AGENT_INVOICE_NUM_RE.match(tokens[0]):
                    cur_cust_num = tokens[0]
                    cur_cust_name, _addr = split_name_from_address_blob(" ".join(tokens[1:]))
                    continue

                if (AGENT_INVOICE_NUM_RE.match(tokens[0]) and len(tokens) > 4
                        and tokens[2] in AGENT_MONTHS and tokens[3].isdigit()):
                    cur_inv_num, cur_inv_date = tokens[0], f"{tokens[1]} {tokens[2]} {tokens[3]}"
                    rest = tokens[4:]
                    if rest and rest[-1] == "USD":
                        rest = rest[:-1]
                    if len(rest) < 3:
                        continue
                    sales, rate, earned = rest[-3], rest[-2], rest[-1]
                    middle = rest[:-3]
                    code_idx = next((i for i, t in enumerate(middle) if AGENT_PRODCODE_RE.match(t)), None)
                    if code_idx is None:
                        continue
                    records.append({
                        "CustomerNumber": cur_cust_num, "CustomerName": cur_cust_name,
                        "Invoice": cur_inv_num, "InvoiceDate": cur_inv_date,
                        "CustomerPO": " ".join(middle[:code_idx]), "ProdLineCode": middle[code_idx],
                        "ProdLineDescription": " ".join(middle[code_idx + 1:]),
                        "SalesAmt": sales, "RatePct": rate, "Earned": earned,
                    })
                    continue

                if AGENT_PRODCODE_RE.match(tokens[0]) and cur_inv_num:
                    rest = tokens[1:]
                    if rest and rest[-1] == "USD":
                        rest = rest[:-1]
                    if len(rest) < 3:
                        continue
                    sales, rate, earned = rest[-3], rest[-2], rest[-1]
                    records.append({
                        "CustomerNumber": cur_cust_num, "CustomerName": cur_cust_name,
                        "Invoice": cur_inv_num, "InvoiceDate": cur_inv_date,
                        "CustomerPO": "", "ProdLineCode": tokens[0],
                        "ProdLineDescription": " ".join(rest[:-3]),
                        "SalesAmt": sales, "RatePct": rate, "Earned": earned,
                    })

    sheet_name = " - ".join([p for p in [company, agent] if p]) or uploaded_file.name
    rows_df = pd.DataFrame(records, columns=AGENT_RECAP_HEADERS + ["_highlighted"])
    rows_df["_highlighted"] = False
    return sheet_name, AGENT_RECAP_HEADERS, rows_df, False  # supports_highlight=False


def extract_pdf_sheet(uploaded_file):
    fmt = detect_pdf_format(uploaded_file)
    if fmt == "agent_recap":
        return extract_pdf_sheet_agent_recap(uploaded_file)
    return extract_pdf_sheet_hhp(uploaded_file)


def is_zero_or_blank(value):
    if is_blank(value):
        return True
    try:
        return float(str(value).replace(",", "")) == 0
    except (ValueError, TypeError):
        return False


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
    with st.expander("💾 Storage status", expanded=False):
        st.caption(f"App folder: `{APP_DIR}`")
        writable = os.access(APP_DIR, os.W_OK)
        if writable:
            st.markdown("✅ Folder is writable — mappings should persist across runs.")
        else:
            st.markdown(
                "🔴 Folder is **not writable** in this environment. Mappings will "
                "reset every time the app restarts. This happens on some hosting "
                "setups with a read-only filesystem — run locally, or point "
                "`MAPPINGS_FILE`/`SHEET_PROFILES_FILE` at a writable/persistent "
                "location for your deployment."
            )
        st.caption(f"`mappings.json`: {len(st.session_state.mappings)} entries")
        st.caption(f"`sheet_profiles.json`: {len(st.session_state.sheet_profiles)} entries")
        st.caption(
            "If this count resets to 0 after you restart the app even though you "
            "clicked **Save mapping**, the app's storage isn't persisting in your "
            "environment — see above."
        )

    with st.expander(f"⚙️ Remembered sheet setups ({len(st.session_state.sheet_profiles)})", expanded=False):
        for norm_name, prof in sorted(st.session_state.sheet_profiles.items()):
            c1, c2 = st.columns([5, 1])
            status = "included" if prof.get("include") else "skipped"
            detail = ""
            if prof.get("include"):
                if prof.get("kind") == "pdf":
                    detail = (
                        f", PDF · drop highlighted: {prof.get('drop_highlighted', True)}"
                        + (f" · skip if `{prof.get('zero_skip_column')}` is 0" if prof.get("zero_skip_column") else "")
                    )
                else:
                    detail = f", header row {prof.get('header_row')}, anchor `{prof.get('anchor_column')}` ({prof.get('anchor_type')})"
            c1.markdown(f"`{norm_name}` — **{status}**{detail}")
            if c2.button("✕", key=f"delprof_{norm_name}", help="Forget this sheet setup"):
                del st.session_state.sheet_profiles[norm_name]
                save_json(SHEET_PROFILES_FILE, st.session_state.sheet_profiles)
                st.rerun()
        if not st.session_state.sheet_profiles:
            st.caption("Nothing remembered yet.")

    with st.expander(f"⚙️ Remembered column mappings ({len(st.session_state.mappings)})", expanded=False):
        if st.session_state.mappings:
            for norm_src in sorted(st.session_state.mappings.keys()):
                target = st.session_state.mappings[norm_src]
                c1, c2 = st.columns([5, 1])
                c1.markdown(f"`{norm_src}` → **{target or '_(ignored)_'}**")
                if c2.button("✕", key=f"delmap_{norm_src}", help="Forget this mapping"):
                    del st.session_state.mappings[norm_src]
                    save_json(MAPPINGS_FILE, st.session_state.mappings)
                    st.rerun()
        else:
            st.caption("Nothing remembered yet.")
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

    st.divider()
    st.header("💾 Backup / restore data")
    st.caption(
        "Your mappings, sheet setups, and standard columns — bundled into one "
        "file. Keep this safe; if the app's data ever gets reset (e.g. an "
        "app.py update accidentally overwrote the JSON files), restore from "
        "here instead of starting over."
    )
    backup_payload = json.dumps({
        "mappings": st.session_state.mappings,
        "sheet_profiles": st.session_state.sheet_profiles,
        "standard_headers": st.session_state.std_headers,
    }, indent=2, ensure_ascii=False)
    st.download_button(
        "⬇ Download backup",
        backup_payload.encode("utf-8"),
        file_name=f"pos_header_mapper_backup_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.json",
        mime="application/json",
    )

    restore_file = st.file_uploader("Restore from a backup file", type=["json"], key="restore_uploader")
    if restore_file is not None:
        try:
            backup_data = json.load(restore_file)
            st.warning(
                f"This backup has {len(backup_data.get('mappings', {}))} mapping(s), "
                f"{len(backup_data.get('sheet_profiles', {}))} sheet setup(s), and "
                f"{len(backup_data.get('standard_headers', []))} standard column(s). "
                "Restoring will overwrite your current data."
            )
            if st.button("Confirm restore"):
                st.session_state.mappings = backup_data.get("mappings", {})
                st.session_state.sheet_profiles = backup_data.get("sheet_profiles", {})
                st.session_state.std_headers = backup_data.get("standard_headers", DEFAULT_STD_HEADERS)
                save_json(MAPPINGS_FILE, st.session_state.mappings)
                save_json(SHEET_PROFILES_FILE, st.session_state.sheet_profiles)
                save_json(STD_HEADERS_FILE, st.session_state.std_headers)
                st.success("Restored.")
                st.rerun()
        except (json.JSONDecodeError, AttributeError):
            st.error("That doesn't look like a valid backup file.")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
st.title("🗂️ POS Header Mapper")
st.caption(
    "Upload one or many POS files, even ones where your data is spread across "
    "several sheets. Set up each sheet type and column mapping once — every "
    "future file with the same shape is merged automatically into one output."
)

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

uc1, uc2 = st.columns([5, 1])
with uc1:
    uploaded_files = st.file_uploader(
        "Upload POS file(s)", type=["csv", "xlsx", "xls", "pdf"], accept_multiple_files=True,
        key=f"uploader_{st.session_state.uploader_key}",
    )
with uc2:
    st.write("")  # vertical spacer to align button with the uploader
    st.write("")
    if st.button("🗑 Clear files"):
        st.session_state.uploader_key += 1
        st.session_state.output_df = None
        st.rerun()

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
            # find one representative entry for this sheet name (from first file that has it)
            entry = next(
                sheets[sheet_name] for sheets in file_sheets.values() if sheet_name in sheets
            )
            kind = entry.get("kind", "excel")
            with st.expander(f"Sheet: {sheet_name} ({'PDF' if kind == 'pdf' else 'spreadsheet'})", expanded=True):
                include = st.checkbox(
                    "Include this sheet's rows in the merge",
                    value=not any(k in normalize(sheet_name) for k in ["summary", "pivot", "index", "readme"]),
                    key=f"inc_{norm_name}",
                )
                if include and kind == "pdf":
                    headers = entry["headers"]
                    supports_highlight = entry.get("supports_highlight", True)
                    if supports_highlight:
                        st.checkbox(
                            "Drop highlighted/colored rows (e.g. tariff or adjustment lines)",
                            value=True, key=f"drophl_{norm_name}",
                        )
                    zero_options = ["— none —"] + headers
                    default_zero_candidates = [h for h in ("PaymentDue", "Earned") if h in headers]
                    default_zero = default_zero_candidates[0] if default_zero_candidates else zero_options[0]
                    st.selectbox(
                        "Skip rows where this column is zero or blank (optional)",
                        zero_options, index=zero_options.index(default_zero),
                        key=f"zeroskip_{norm_name}",
                    )
                    st.caption(f"Columns extracted from this PDF: {', '.join(headers)}")
                elif include:
                    sample_df = entry["raw_df"]
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
                widget_state[norm_name] = (sheet_name, kind)

        if st.button("Save sheet setup", type="primary"):
            for norm_name, (sheet_name, kind) in widget_state.items():
                include = st.session_state.get(f"inc_{norm_name}", False)
                if not include:
                    st.session_state.sheet_profiles[norm_name] = {
                        "display_name": sheet_name, "include": False, "kind": kind,
                    }
                elif kind == "pdf":
                    zero_choice = st.session_state.get(f"zeroskip_{norm_name}", "— none —")
                    st.session_state.sheet_profiles[norm_name] = {
                        "display_name": sheet_name,
                        "include": True,
                        "kind": "pdf",
                        "drop_highlighted": st.session_state.get(f"drophl_{norm_name}", True),
                        "zero_skip_column": "" if zero_choice == "— none —" else zero_choice,
                    }
                else:
                    st.session_state.sheet_profiles[norm_name] = {
                        "display_name": sheet_name,
                        "include": True,
                        "kind": "excel",
                        "header_row": st.session_state.get(f"hr_{norm_name}", 1),
                        "anchor_column": st.session_state.get(f"anchor_{norm_name}"),
                        "anchor_type": st.session_state.get(f"anchortype_{norm_name}", "text"),
                    }
            save_json(SHEET_PROFILES_FILE, st.session_state.sheet_profiles)
            st.rerun()

    else:
        # 3) All sheets resolved — extract rows from every included sheet of every file
        extracted = []  # list of (filename, sheet_name, headers, df_rows)
        for fname, sheets in file_sheets.items():
            for sheet_name, entry in sheets.items():
                prof = st.session_state.sheet_profiles.get(normalize(sheet_name))
                if not prof or not prof.get("include"):
                    continue
                kind = prof.get("kind", entry.get("kind", "excel"))

                if kind == "pdf":
                    headers = entry["headers"]
                    rows_df = entry["rows_df"].copy()
                    if prof.get("drop_highlighted", True) and "_highlighted" in rows_df.columns:
                        rows_df = rows_df[~rows_df["_highlighted"].astype(bool)]
                    zero_col = prof.get("zero_skip_column")
                    if zero_col and zero_col in rows_df.columns:
                        rows_df = rows_df[~rows_df[zero_col].apply(is_zero_or_blank)]
                    rows_df = rows_df.drop(columns=["_highlighted"], errors="ignore").reset_index(drop=True)
                    extracted.append((fname, sheet_name, headers, rows_df))
                else:
                    raw_df = entry["raw_df"]
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
            mappings_saved_ok = save_json(MAPPINGS_FILE, st.session_state.mappings)

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
            final_df = format_output_df(final_df, st.session_state.std_headers)
            final_df = final_df.fillna("")
            st.session_state.output_df = final_df
            if mappings_saved_ok:
                st.success(
                    f"Merged. {len(final_df)} rows ready to download below. "
                    f"{len(mapping_choices)} column mapping(s) saved for next time."
                )
            else:
                st.warning(f"Merged. {len(final_df)} rows ready — but see the storage warning above.")


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
