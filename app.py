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


class SmartProgressUI:
    def __init__(self, placeholder, total_mps):
        self.placeholder = placeholder
        self.total_mps = max(1, total_mps)
        self.current_mp_index = 0
        with self.placeholder.container():
            self.overall_bar = st.progress(0.0, text="Starting search...")
            self.status_box = st.info("Initializing search engine...")
            self.detail_caption = st.caption("Querying NSW Hansard...")
        self.log_lines = []
        self.current_line = ""

    def write(self, text):
        if not text:
            return 0
        text_str = str(text).replace("\\n", "\n")
        for char in text_str:
            if char in ("\n", "\r"):
                if self.current_line:
                    clean = re.sub(r"\x1b\[.*?m", "", self.current_line).strip()
                    if clean:
                        self.log_lines.append(clean)
                        self._process_line(clean)
                self.current_line = ""
            else:
                self.current_line += char
        return len(text)

    def _process_line(self, line):
        mp_match = re.search(r"(?:using|searching|for MP)\s*['\"]([^'\"]+)['\"]", line, re.IGNORECASE)
        if mp_match:
            mp_name = mp_match.group(1)
            self.current_mp_index = min(self.current_mp_index + 1, self.total_mps)
            self.status_box.info(f"Searching MP ({self.current_mp_index}/{self.total_mps}): {mp_name}")

        frag_match = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
        sub_progress = 0.0
        if frag_match:
            c = int(frag_match.group(1))
            t = int(frag_match.group(2))
            if t > 0:
                sub_progress = min(1.0, c / t)
                self.detail_caption.caption(f"Downloading fragments: {c:,} of {t:,}")

        # Compute safe percentage using split lines
        completed_mps = max(0, self.current_mp_index - 1)
        base_pct = completed_mps / self.total_mps
        step_pct = (1.0 / self.total_mps) * sub_progress
        overall_pct = base_pct + step_pct
        overall_pct = min(0.99, max(0.02, overall_pct))

        self.overall_bar.progress(
            overall_pct,
            text=f"Progress: {int(overall_pct * 100)}% ({self.current_mp_index}/{self.total_mps} MPs)",
        )

    def flush(self):
        pass

    def complete(self):
        self.overall_bar.progress(1.0, text="Search complete!")
        self.status_box.success("Extraction finished successfully.")
        self.detail_caption.empty()

    def get_logs(self):
        return "\n".join(self.log_lines)


st.set_page_config(page_title="NSW Hansard Research Tool", page_icon="🏛️", layout="wide")
st.title("NSW Hansard Research Tool")
st.caption("Configure the search, run the Hansard scraper, and download an Excel workbook.")


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

st.subheader("1. Choose MPs")
filtered_mp_df = mp_df[mp_df["chamber"].isin(chambers)]
all_visible = filtered_mp_df["display"].tolist()

col_a, col_b = st.columns([1, 5])
with col_a:
    clear_all = st.button("Deselect all MPs")

selected_display = st.multiselect(
    "MPs to search",
    options=all_visible,
    default=[] if clear_all else all_visible,
    help="Tip: Select only 1 or 2 MPs first to test your keywords quickly!",
)
selected_names = [display_to_name[x] for x in selected_display]

st.subheader("2. Configure keyword matching")
keyword_col1, keyword_col2 = st.columns(2)
with keyword_col1:
    use_direct = st.checkbox("Include direct keywords", value=True)
    direct_text = st.text_area(
        "Direct keyword list (one per line)",
        value="\n".join(DIRECT_KEYWORDS),
        height=200,
    )
with keyword_col2:
    use_euphemisms = st.checkbox("Include euphemism / indirect keywords", value=True)
    euphemism_text = st.text_area(
        "Euphemism keyword list (one per line)",
        value="\n".join(EUPHEMISM_KEYWORDS),
        height=200,
    )

st.subheader("3. Optional refinements")
with st.expander("Advanced settings"):
    max_workers = st.slider(
        "Concurrent fragment requests",
        min_value=1,
        max_value=10,
        value=6,
        help="Higher values download fragments faster.",
    )
    show_preview = st.checkbox("Show a preview of extracted quotes", value=True)
    include_audit = st.checkbox("Include speaker-label audit in workbook", value=True)

if start_date > end_date:
    st.error("The start date must be on or before the end date.")
    st.stop()
if not selected_names:
    st.warning("Select at least one MP.")
if not chambers:
    st.warning("Select at least one chamber.")

if st.button("Generate spreadsheet", type="primary", use_container_width=True, disabled=(not selected_names or not chambers)):
    direct = [x.strip() for x in direct_text.splitlines() if x.strip()] if use_direct else []
    euphemisms = [x.strip() for x in euphemism_text.splitlines() if x.strip()] if use_euphemisms else []

    keywords = []
    seen = set()
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

        st.markdown("---")
        progress_placeholder = st.empty()
        ui = SmartProgressUI(progress_placeholder, total_mps=len(selected_names))

        try:
            with contextlib.redirect_stdout(ui), contextlib.redirect_stderr(ui):
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

            ui.complete()

            df = pd.read_csv(quotes_path) if quotes_path.exists() else pd.DataFrame()
            audit_df = pd.read_csv(audit_path) if audit_path.exists() and include_audit else pd.DataFrame()

            if df.empty:
                st.warning("The scraper finished but found zero keyword matches across the transcripts.")
            else:
                st.success(f"Found {len(df):,} quote rows across {df['mp_name'].nunique():,} MPs!")
                if show_preview:
                    st.dataframe(df.head(100), use_container_width=True, hide_index=True)

            excel_buffer = io.BytesIO()
            settings_df = pd.DataFrame(
                {
                    "Setting": [
                        "Start date",
                        "End date",
                        "MPs searched",
                        "Chambers",
                        "Direct keywords enabled",
                        "Euphemism keywords enabled",
                        "Concurrent requests",
                    ],
                    "Value": [
                        start_date.isoformat(),
                        end_date.isoformat(),
                        "; ".join(selected_names),
                        "; ".join(chambers),
                        str(use_direct),
                        str(use_euphemisms),
                        max_workers,
                    ],
                }
            )
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

            with st.expander("Show detailed scraper log"):
                st.text(ui.get_logs()[-20000:])

        except Exception as exc:
            st.error("The scraper encountered an unexpected error.")
            st.exception(exc)
            with st.expander("Show detailed scraper log"):
                st.text(ui.get_logs()[-20000:])
