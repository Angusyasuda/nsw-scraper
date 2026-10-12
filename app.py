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
    """Tracks overall MP completion and fragment downloads cleanly."""
    def __init__(self, placeholder, total_mps):
        self.placeholder = placeholder
        self.total_mps = max(1, total_mps)
        self.current_mp_index = 0
        
        with self.placeholder.container():
            self.overall_bar = st.progress(0.0, text=f"Starting search across {self.total_mps} MP(s)...")
            self.status_box = st.info("Initializing search engine...")
            self.detail_caption = st.caption("Preparing to query NSW Hansard database...")
            
        self.log_lines = []
        self.current_line = ""

    def write(self, text):
        if not text:
            return 0
            
        text_str = str(text).replace("\\n", "\n")
        
        for char in text_str:
            if char in ('\n', '\r'):
                if self.current_line:
                    clean = re.sub(r'\x1b\[.*?m', '', self.current_line).strip()
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
            self.status_box.info(f"🏛️ **Searching MP ({self.current_mp_index}/{self.total_mps}):** `{mp_name}`")
            
        frag_match = re.search(r'\[(\d+)\s*/\s*(\d+)\]', line)
        sub_progress = 0.0
        if frag_match:
            c, t = int(frag_match.group(1)), int(frag_match.group(2))
            if t > 0:
                sub_progress = min(1.0, c / t)
                self.detail_caption.caption(f"📥 Downloading transcript fragments: **{c:,} of {t:,}**")

        base_pct = (max(0, self.current_mp_index -
