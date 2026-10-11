# NSW Hansard Research Tool

A Streamlit web interface around the existing NSW Hansard wage-policy scraper.

## Deploy through GitHub + Streamlit Community Cloud

1. Upload `app.py`, `hansard_scraper.py`, and `requirements.txt` to the GitHub repository.
2. In Streamlit Community Cloud, create an app from that repository.
3. Set the main file path to `app.py`.
4. Deploy. Streamlit installs packages from `requirements.txt`.

## Features
- Choose date range.
- Filter and select individual MPs from the MP list inherited from the original notebook.
- Edit direct and euphemism keyword lists at runtime.
- Adjust concurrency conservatively.
- Preview extracted quotes.
- Download an Excel workbook with Quotes, Search settings, Keywords used, and optional Speaker audit tabs.

The app applies settings at runtime rather than rewriting its own Python source code. Verify results against the original notebook before relying on them for research.
