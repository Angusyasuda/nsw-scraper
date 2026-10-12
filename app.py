from datetime import date
from pathlib import Path
import contextlib
import io
import tempfile

import pandas as pd
import streamlit as st

from hansard_scraper import (
    DIRECT_KEYWORDS,
    EUPHEMISM_KEYWORDS,
    MPS_CSV,
    run_scraper,
)


class LiveLog:
    """Stream scraper stdout into a visible Streamlit log panel."""
    def __init__(self, placeholder, max_lines=35):
        self.placeholder = placeholder
        self.max_lines = max_lines
        self.buffer = ""
        self.lines = []

    def write(self, text):
        if not text:
            return
        self.buffer += str(text)
        while "\\n" in self.buffer:
            line, self.buffer = self.buffer.split("\\n", 1)
            self.lines.append(line)
            self.placeholder.code("\\n".join(self.lines[-self.max_lines:]), language=None)
        return len(text)

    def flush(self):
        if self.buffer:
            self.lines.append(self.buffer)
            self.buffer = ""
            self.placeholder.code("\\n".join(self.lines[-self.max_lines:]), language=None)


st.set_page_config(page_title="NSW Hansard Research Tool", page_icon="🏛️", layout="wide")
st.title("NSW Hansard Research Tool")
st.caption("Configure the search, run the Hansard scraper, and download an Excel workbook.")

# Parse the MP records already present in the scraper, without changing the source list.
@st.cache_data
def get_mp_records():
    from io import StringIO
    return pd.read_csv(StringIO(MPS_CSV)).fillna("")

mp_df = get_mp_records()
mp_df["display"] = mp_df.apply(
    lambda r: f'{r["search_name"]} — {r["chamber"]} — {r["role_notes"]}', axis=1
)
display_to_name = dict(zip(mp_df["display"], mp_df["search_name"]))

with st.sidebar:
    st.header("Search settings")
    start_date = st.date_input("Start date", value=date(2023, 3, 1)
    )
    end_date = st.date_input("End date", value=date(2026, 9, 24))
    chambers = st.multiselect(
        "Chambers",
        options=["LH", "UH"],
        default=["LH", "UH"],
        format_func=lambda x: {"LH": "Legislative Assembly", "UH": "Legislative Council"}[x],
    )
    st.caption("These chamber filters use the chamber labels stored in your existing MP list.")

st.subheader("1. Choose MPs")
filtered_mp_df = mp_df[mp_df["chamber"].isin(chambers)]
all_visible = filtered_mp_df["display"].tolist()
default_selected = all_visible
selected_display = st.multiselect(
    "MPs to search",
    options=all_visible,
    default=default_selected,
    help="Choose individual MPs. Clear the selection and select only the people you need.",
)
selected_names = [display_to_name[x] for x in selected_display]

with st.expander("View selected MP details"):
    if selected_display:
        st.dataframe(
            filtered_mp_df[filtered_mp_df["display"].isin(selected_display)][
                ["search_name", "chamber", "role_notes"]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No MPs selected.")

st.subheader("2. Configure keyword matching")
keyword_col1, keyword_col2 = st.columns(2)
with keyword_col1:
    use_direct = st.checkbox("Include direct keywords", value=True)
    direct_text = st.text_area(
        "Direct keyword list (one per line)",
        value="\n".join(DIRECT_KEYWORDS),
        height=260,
        help="Edit, remove or add phrases. Matching is case-insensitive and checks for phrase occurrence.",
    )
with keyword_col2:
    use_euphemisms = st.checkbox("Include euphemism / indirect keywords", value=True)
    euphemism_text = st.text_area(
        "Euphemism keyword list (one per line)",
        value="\n".join(EUPHEMISM_KEYWORDS),
        height=260,
        help="Use this for indirect or politically framed language, not only explicit wage-policy terms.",
    )

st.subheader("3. Optional refinements")
with st.expander("Advanced settings"):
    max_workers = st.slider(
        "Concurrent fragment requests", min_value=1, max_value=8, value=5,
        help="Higher values may run faster but can increase load or API throttling. The default is conservative.",
    )
    show_preview = st.checkbox("Show a preview of extracted quotes", value=True)
    include_audit = st.checkbox("Include speaker-label audit in workbook", value=True)

st.caption(
    "The app applies your settings at runtime; it does not rewrite the Python source code for each run. "
    "That is safer and makes each search repeatable."
)

if start_date > end_date:
    st.error("The start date must be on or before the end date.")
    st.stop()
if not selected_names:
    st.warning("Select at least one MP.")
if not chambers:
    st.warning("Select at least one chamber.")

if st.button("Generate spreadsheet", type="primary", use_container_width=True,
             disabled=(not selected_names or not chambers)):
    direct = [x.strip() for x in direct_text.splitlines() if x.strip()] if use_direct else []
    euphemisms = [x.strip() for x in euphemism_text.splitlines() if x.strip()] if use_euphemisms else []
    # De-duplicate case-insensitively while preserving the first occurrence and category.
    keywords, seen = [], set()
    for phrase, kind in [(x, "direct") for x in direct] + [(x, "euphemism") for x in euphemisms]:
        key = phrase.casefold()
        if key not in seen:
            seen.add(key)
            keywords.append((phrase, kind))

    if not keywords:
        st.error("Enable at least one keyword category and include at least one keyword.")
        st.stop()

    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        mps_path = temp / "selected_mps.csv"
        quotes_path = temp / "wage_quotes.csv"
        audit_path = temp / "speaker_label_audit.csv"
        # Write only selected MPs; the scraper also receives the selected names for safety.
        chosen = mp_df[mp_df["search_name"].isin(selected_names)][
            ["search_name", "last_name", "chamber", "role_notes"]
        ]
        chosen.to_csv(mps_path, index=False)
        log_stream = io.StringIO()
        status = st.status("Starting Hansard search… live progress will appear below.", expanded=True)
        st.caption(
            "Large searches can take several minutes. The live log below shows which MP is being "
            "searched and whether fragments are being downloaded. If the log stops on an API request, "
            "check the deployment logs for a timeout or API error."
        )
        live_log_area = st.empty()
        live_log = LiveLog(live_log_area)
        try:
            with contextlib.redirect_stdout(live_log):
                run_scraper(
                    start=start_date.isoformat(),
                    end=end_date.isoformat(),
                    mps_file=str(mps_path),
                    out_file=str(quotes_path),
                    audit_file=str(audit_path),
                    selected_mps=selected_names,
                    keywords=keywords,
                    max_workers=max_workers,
                )
            live_log.flush()
            log_stream.write("\\n".join(live_log.lines))
            status.update(label="Search complete", state="complete", expanded=False)
            df = pd.read_csv(quotes_path) if quotes_path.exists() else pd.DataFrame()
            audit_df = pd.read_csv(audit_path) if audit_path.exists() and include_audit else pd.DataFrame()
            if df.empty:
                st.warning("The scraper completed but found no matching quotes for these settings.")
            else:
                st.success(f"Found {len(df):,} quote rows across {df['mp_name'].nunique():,} MPs.")
                if show_preview:
                    st.dataframe(df.head(100), use_container_width=True, hide_index=True)

            # Deliver a single Excel workbook with quote results, settings, and optional audit.
            excel_buffer = io.BytesIO()
            settings_df = pd.DataFrame({
                "Setting": ["Start date", "End date", "MPs searched", "Chambers", "Direct keywords enabled",
                            "Euphemism keywords enabled", "Concurrent requests"],
                "Value": [start_date.isoformat(), end_date.isoformat(), "; ".join(selected_names),
                          "; ".join(chambers), str(use_direct), str(use_euphemisms), max_workers],
            })
            keyword_df = pd.DataFrame(keywords, columns=["keyword", "keyword_type"])
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="Quotes", index=False)
                settings_df.to_excel(writer, sheet_name="Search settings", index=False)
                keyword_df.to_excel(writer, sheet_name="Keywords used", index=False)
                if not audit_df.empty:
                    audit_df.to_excel(writer, sheet_name="Speaker audit", index=False)
            st.download_button(
                "Download Excel workbook (.xlsx)",
                data=excel_buffer.getvalue(),
                file_name=f"hansard_quotes_{start_date}_{end_date}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            with st.expander("Run log and diagnostics"):
                st.text("\\n".join(live_log.lines)[-20000:] or log_stream.getvalue()[-20000:] or "No log output captured.")
        except Exception as exc:
            status.update(label="Search failed", state="error", expanded=True)
            st.error("The scraper encountered an error. Review the details below.")
            st.exception(exc)
            with st.expander("Captured run log"):
                st.text("\\n".join(live_log.lines)[-20000:] or log_stream.getvalue()[-20000:] or "No log output captured.")
