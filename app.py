from datetime import date
from pathlib import Path
import contextlib
import io
import tempfile
import re

import pandas as pd
import streamlit as st

from hansard_scraper import (
    DIRECT_KEYWORDS,
    EUPHEMISM_KEYWORDS,
    MPS_CSV,
    run_scraper,
)


class ModernProgressUI:
    """Intercepts terminal output to drive native Streamlit UI elements."""
    def __init__(self, placeholder):
        self.placeholder = placeholder
        self.progress_bar = self.placeholder.progress(0.0, text="Initializing search...")
        self.status_text = self.placeholder.empty()
        self.log_lines = []
        self.current_line = ""

    def write(self, text):
        if not text:
            return 0
            
        # Standardize newlines
        text_str = str(text).replace("\\n", "\n")
        
        for char in text_str:
            if char == '\n' or char == '\r':
                if self.current_line:
                    # Remove terminal color codes (ANSI escapes)
                    clean_line = re.sub(r'\x1b\[.*?m', '', self.current_line).strip()
                    
                    if clean_line:
                        self.log_lines.append(clean_line)
                        
                        # 1. Look for progress numbers like [260/462] or 45%
                        fraction = re.search(r'(\d+)\s*/\s*(\d+)', clean_line)
                        percent = re.search(r'(\d{1,3})%', clean_line)
                        
                        pct = None
                        if fraction:
                            c, t = int(fraction.group(1)), int(fraction.group(2))
                            if t > 0 and c <= t:
                                pct = c / t
                        elif percent:
                            pct = int(percent.group(1)) / 100.0
                            
                        # Update the Streamlit Progress Bar
                        if pct is not None:
                            pct = max(0.0, min(float(pct), 1.0)) # Keep between 0 and 1
                            self.progress_bar.progress(pct, text="Scraping in progress...")
                            
                        # 2. Update the text label (only if it contains letters, ignoring pure progress bars)
                        if re.search(r'[a-zA-Z]', clean_line):
                            self.status_text.caption(f"🔄 {clean_line[:120]}")
                            
                # Reset line buffer on \n or \r
                self.current_line = ""
            else:
                self.current_line += char

        return len(text)

    def flush(self):
        pass
        
    def get_logs(self):
        return "\n".join(self.log_lines)


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
    start_date = st.date_input("Start date", value=date(2023, 3, 1))
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
        
        chosen = mp_df[mp_df["search_name"].isin(selected_names)][
            ["search_name", "last_name", "chamber", "role_notes"]
        ]
        chosen.to_csv(mps_path, index=False)
        
        # --- NEW UI CONTAINER ---
        st.markdown("---")
        ui_container = st.container()
        progress_ui = ModernProgressUI(ui_container)
        
        try:
            # Capture both stdout and stderr (where progress bars print) to our new UI
            with contextlib.redirect_stdout(progress_ui), contextlib.redirect_stderr(progress_ui):
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
                
            # Finish up UI
            progress_ui.progress_bar.progress(1.0, text="Search complete!")
            progress_ui.status_text.caption("✅ Done.")
            
            df = pd.read_csv(quotes_path) if quotes_path.exists() else pd.DataFrame()
            audit_df = pd.read_csv(audit_path) if audit_path.exists() and include_audit else pd.DataFrame()
            
            if df.empty:
                st.warning("The scraper completed but found no matching quotes for these settings.")
            else:
                st.success(f"Found {len(df):,} quote rows across {df['mp_name'].nunique():,} MPs.")
                if show_preview:
                    st.dataframe(df.head(100), use_container_width=True, hide_index=True)

            # Deliver Excel workbook
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
            
            with st.expander("View detailed scraper logs (for debugging)"):
                st.text(progress_ui.get_logs()[-20000:] or "No log output captured.")
                
        except Exception as exc:
            progress_ui.progress_bar.progress(1.0, text="Search failed.")
            progress_ui.status_text.caption("❌ Error occurred.")
            st.error("The scraper encountered an error. Review the details below.")
            st.exception(exc)
            with st.expander("Captured run log"):
                st.text(progress_ui.get_logs()[-20000:] or "No log output captured.")
