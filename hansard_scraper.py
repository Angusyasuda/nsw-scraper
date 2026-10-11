import concurrent.futures
import csv
import json
import os
import re
import time
from collections import Counter
from html import unescape

import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString

# 2. CONSTANTS
BASE = "https://api.parliament.nsw.gov.au/api"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "nsw-wage-policy-research/2.1"})
REQUEST_DELAY = 0.05
MAX_RETRIES = 3

DIRECT_KEYWORDS = [
    "wage cap", "wages cap", "pay cap",
    "wage freeze", "wages freeze", "pay freeze",
    "public sector wage", "public sector wages", "public sector pay",
    "public servant pay", "public servant wage",
    "government employee wage", "government employee pay",
    "industrial relations reform",
    "Industrial Relations Commission", "IRC wage", "IRC decision",
    "productivity offset", "productivity trade-off", "productivity saving",
    "wage affordability",
    "spending on wages", "wages bill",
    "budget impact of wage", "budget impacts of wage",
    "wages policy", "wage policy",
    "public sector pay rise", "public sector pay increase",
    "removing the wages cap", "removing the wage cap",
    "scrapping the wages cap", "scrapping the wage cap",
    "award wage", "enterprise agreement",
]
EUPHEMISM_KEYWORDS = [
    "budget discipline", "financial sustainability", "responsible wages policy",
    "responsible wage policy", "productivity savings", "unfunded commitment",
    "unfunded commitments", "cost pressures", "government affordability",
    "fiscal discipline", "wages blowout", "wages spiral",
]
KEYWORDS = [(k, "direct") for k in DIRECT_KEYWORDS] + \
            [(k, "euphemism") for k in EUPHEMISM_KEYWORDS]

TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "professor", "hon", "the",
          "rev", "reverend", "sir", "dame", "mp", "mlc"}
TOKEN = re.compile(r"[a-z]+(?:['-][a-z]+)*")
LABEL_START = re.compile(
    r"^\s*(the\s+hon\.?|hon\.?|mr|mrs|ms|miss|dr|prof(essor)?|rev(erend)?|the)\b",
    re.I)
BLOCK_TAGS = ["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"]
BOLD_TAGS = ("strong", "b")

# 3. CORE LOGIC
def get_json(url, method="GET", **kwargs):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_DELAY)
            resp = SESSION.request(method, url, timeout=30, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json(), None
        except requests.exceptions.HTTPError as e:
            last_err = f"HTTP Error {e.response.status_code} for URL: {e.response.url}"
            return None, last_err
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    return None, last_err

def walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_dicts(v)

def unwrap(data):
    if isinstance(data, dict):
        for key in ("Results", "results", "Items", "items", "Data", "data"):
            if key in data:
                return data[key]
    return data

def collect_fragment_ids_and_dates(data):
    results = {}

    def traverse(obj, current_date):
        if isinstance(obj, dict):
            node_date = current_date
            for k, v in obj.items():
                # Specifically look for date-like structures, but avoid generic top-level search dates if possible
                if isinstance(k, str) and k.lower() in ["date", "sittingdate", "documentdate", "eventdate"]:
                    if isinstance(v, str) and re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", v):
                        node_date = v[:10].replace("/", "-")

            for k, v in obj.items():
                if isinstance(v, str):
                    match = re.search(r"HANSARD-\d+-\d+", v)
                    if match:
                        results[match.group(0)] = node_date

            for v in obj.values():
                traverse(v, node_date)
        elif isinstance(obj, list):
            for item in obj:
                traverse(item, current_date)

    traverse(data, "")
    return results

def api_date(d):
    return d.replace("-", "/")

FRAGMENT_ENDPOINTS = [
    ("POST", "/hansard/search/daily/fragment/html/{id}"),
    ("GET",  "/hansard/search/daily/fragment/html/{id}"),
    ("GET",  "/hansard/document/fragment/{id}"),
]
WORKING_ENDPOINT = {}

def html_from(doc):
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except json.JSONDecodeError:
            match = re.search(r'"(?:DocumentHtml|Html|html|Content)"\s*:\s*"(.*?)"(?:\}|$)', doc, re.DOTALL)
            if match:
                extracted = match.group(1)
                try:
                    return extracted.encode('utf-8').decode('unicode_escape')
                except:
                    return extracted.replace('\"', '"').replace('\n', '\n').replace('\\/', '/')
    if isinstance(doc, str):
        return doc
    for d in walk_dicts(doc):
        for k in ("Html", "html", "HTML", "Content", "content", "Body", "body", "DocumentHtml", "documentHtml"):
            v = d.get(k)
            if isinstance(v, str) and "<" in v:
                return v
    return ""

def fetch_fragment(frag_id, errors):
    order = list(FRAGMENT_ENDPOINTS)
    if WORKING_ENDPOINT:
        order = [WORKING_ENDPOINT["ep"]] + [e for e in order if e != WORKING_ENDPOINT["ep"]]
    last = None
    for method, path in order:
        doc, err = get_json(BASE + path.format(id=frag_id), method=method)
        if err:
            last = f"{method} {path}: {err}"
            continue
        html = html_from(doc)
        if html:
            WORKING_ENDPOINT["ep"] = (method, path)
            return doc, html
        last = f"{method} {path}: response had no HTML"
    errors.append(last)
    return None, ""

def load_mps(path):
    mps = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            parts = [p.strip() for p in row["search_name"].split(",")]
            row["first_name"] = parts[-1] if len(parts) >= 2 else ""
            mps.append(row)
    return mps

def name_tokens(text):
    text = re.sub(r"\(.*\)|\[.*\]", " " , text)
    text = text.lower().replace("’", "'")
    return [t for t in TOKEN.findall(text) if t not in TITLES]

def label_matches_mp(label, mp):
    toks = name_tokens(label)
    if not toks:
        return False
    first = mp["first_name"].lower()
    last = mp["last_name"].lower()
    if len(toks) == 1 and toks[0] == last:
        return True
    return len(toks) >= 2 and toks[0] == first and toks[-1] == last

def label_mentions_surname(label, mp):
    return mp["last_name"].lower() in name_tokens(label)

def leaf_blocks(soup):
    blocks = []
    for el in soup.find_all(BLOCK_TAGS):
        if el.name.startswith("h") or el.find(["p", "div", "li"]) is None:
            blocks.append(el)
    return blocks

def in_bold(node, stop):
    for parent in node.parents:
        if parent is stop:
            return False
        if getattr(parent, "name", None) in BOLD_TAGS:
            return True
        if getattr(parent, "name", None) == "span":
            classes = parent.get("class", [])
            if classes and ("MemberSpeech-H" in classes or "GeneralBold-H" in classes):
                return True
    return False

def split_leading_label(block):
    label_parts = []
    started_body = False
    body_parts = []
    for s in block.find_all(string=True):
        if not isinstance(s, NavigableString):
            continue
        txt = str(s)
        if not txt.strip():
            if started_body:
                body_parts.append(txt)
            continue
        if not started_body and in_bold(s, block):
            label_parts.append(txt)
        else:
            started_body = True
            body_parts.append(txt)
    label = " ".join(" ".join(label_parts).split()) or None
    body = " ".join("".join(body_parts).split())
    return label, body

def has_midparagraph_speaker_label(block):
    seen_body = False
    for s in block.find_all(string=True):
        txt = str(s)
        if not txt.strip():
            continue
        if in_bold(s, block):
            if seen_body and LABEL_START.match(txt.strip()) and txt.strip().endswith(":"):
                return True
        else:
            seen_body = True
    return False

def strip_interjections(block):
    for el in block.find_all(["i", "em"]):
        t = el.get_text(" ", strip=True).lower()
        if "interject" in t or "interruption" in t:
            el.decompose()

def find_keyword(text, keywords=None):
    low = text.lower()
    for kw, kind in (keywords if keywords is not None else KEYWORDS):
        if kw.lower() in low:
            return kw, kind
    return None, None

def matched_sentence(text, kw):
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if kw.lower() in sent.lower():
            return sent
    return text

def extract_quotes(html, mp, keywords=None):
    soup = BeautifulSoup(html, "html.parser")
    stats = Counter()
    near_misses = Counter()
    quotes = []
    current = None
    current_label = None
    for idx, block in enumerate(leaf_blocks(soup)):
        if block.name.startswith("h"):
            current, current_label = None, None
            continue
        strip_interjections(block)
        label, body = split_leading_label(block)
        if label:
            stats["labels_seen"] += 1
            if label_matches_mp(label, mp):
                current, current_label = "target", label
                stats["target_turns"] += 1
            else:
                if label_mentions_surname(label, mp):
                    near_misses[label] += 1
                if not LABEL_START.match(label):
                    stats["non_name_bold_breaks"] += 1
                current, current_label = "other", label
            body = body.lstrip(": ").strip()
        if current != "target" or not body:
            continue
        if has_midparagraph_speaker_label(block):
            stats["skipped_mixed_speaker_paragraph"] += 1
            continue
        kw, kind = find_keyword(body, keywords=keywords)
        if kw:
            quotes.append({
                "hansard_speaker_label": current_label,
                "keyword": kw,
                "keyword_type": kind,
                "matched_sentence": matched_sentence(body, kw),
                "quote": body,
                "paragraph_index": idx,
            })
    if stats["labels_seen"] == 0:
        stats["fragment_without_speaker_labels"] += 1
    return quotes, stats, near_misses

FIELDS = ["mp_name", "chamber", "role_current", "date", "fragment_id",
          "hansard_speaker_label", "keyword", "keyword_type",
          "matched_sentence", "quote", "paragraph_index"]

def run_scraper(start="2023-03-01", end="2026-09-24", mps_file="mps.csv",
                out_file="wage_quotes.csv", audit_file="speaker_label_audit.csv",
                selected_mps=None, keywords=None, max_workers=5):
    mps = load_mps(mps_file)
    if selected_mps:
        selected = set(selected_mps)
        mps = [mp for mp in mps if mp["search_name"] in selected]
    if not mps:
        raise ValueError("No MPs selected for scraping.")
    if not keywords:
        raise ValueError("No keywords selected. Add at least one keyword.")
    print(f"Searching {len(mps)} MPs with {len(keywords)} keywords from {start} to {end}.", flush=True)
    print(f"Loaded {len(mps)} MPs from {mps_file}\n", flush=True)
    fragment_cache = {}
    totals = Counter()
    audit = Counter()
    api_errors = []
    seen = set()
    with open(out_file, "w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=FIELDS)
        writer.writeheader()
        for mp in mps:
            print(f"=== {mp['search_name']} ===", flush=True)
            ids_and_dates = {}
            search_attempts = [mp["search_name"], mp["last_name"]]

            for attempt_query in search_attempts:
                print(f"  [1/2] Attempting search for '{attempt_query}'...", flush=True)
                params = {"speakerName": attempt_query, "startDate": api_date(start), "endDate": api_date(end)}
                data, err = get_json(f"{BASE}/hansard/search/byspeaker", params=params)
                if err:
                    api_errors.append((mp["search_name"], f"search ({attempt_query})", err))
                    print(f"  [API ERROR] search failed for '{attempt_query}': {err}", flush=True)
                    continue

                ids_and_dates = collect_fragment_ids_and_dates(unwrap(data))
                if ids_and_dates:
                    print(f"  [2/2] Found {len(ids_and_dates)} fragments using '{attempt_query}'.", flush=True)
                    break
                else:
                    print(f"  [2/2] 0 fragments returned for '{attempt_query}'.", flush=True)

            ids = list(ids_and_dates.keys())

            if not ids:
                print(f"  [2/2] No fragments found for {mp['search_name']} after all search attempts.", flush=True)
                continue

            print(f"  Downloading {len(ids)} fragments concurrently... ", end="", flush=True)
            completed = 0
            mp_quotes = 0
            def fetch_wrapper(fid):
                if fid in fragment_cache:
                    return fid, fragment_cache[fid][0], fragment_cache[fid][1], []
                errs = []
                d, h = fetch_fragment(fid, errs)
                return fid, d, h, errs
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 8))) as executor:
                futures = {executor.submit(fetch_wrapper, fid): fid for fid in ids}
                for future in concurrent.futures.as_completed(futures):
                    completed += 1
                    if completed % 10 == 0:
                        print(f"[{completed}/{len(ids)}] ", end="", flush=True)
                    fid, d, h, errs = future.result()
                    if fid not in fragment_cache:
                        fragment_cache[fid] = (d, h)
                        for e in errs:
                            api_errors.append((mp["search_name"], fid, e))
            for frag_id in ids:
                doc, html = fragment_cache[frag_id]
                if not html:
                    totals["fragments_without_html"] += 1
                    continue

                # Strongly prefer the exact date returned inside the fetched fragment payload
                exact_date = ""
                if isinstance(doc, dict):
                    for d_in in walk_dicts(doc):
                        for k, v in d_in.items():
                            if isinstance(k, str) and k.lower() in ["date", "sittingdate", "documentdate", "eventdate"]:
                                if isinstance(v, str) and re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", v):
                                    exact_date = str(v)[:10].replace("/", "-")
                                    break
                        if exact_date: break
                elif isinstance(doc, str):
                    match = re.search(r'"(?:date|sittingDate|documentDate|eventDate)"\s*:\s*"(\d{4}[-/]\d{2}[-/]\d{2})', doc, re.IGNORECASE)
                    if match:
                        exact_date = match.group(1).replace("/", "-")

                # Fallback to the search payload date if the document itself doesn't provide one
                if not exact_date:
                    exact_date = ids_and_dates.get(frag_id, "")

                quotes, stats, near = extract_quotes(html, mp, keywords=keywords)
                totals.update(stats)
                for label, n in near.items():
                    audit[(mp["search_name"], label)] += n

                for q in quotes:
                    key = (mp["search_name"], frag_id, q["paragraph_index"])
                    if key in seen:
                        continue
                    seen.add(key)
                    writer.writerow({
                        "mp_name": mp["search_name"], "chamber": mp["chamber"],
                        "role_current": mp["role_notes"], "date": exact_date,
                        "fragment_id": frag_id, **q,
                    })
                    mp_quotes += 1
            print(f"\n  Finished: {len(ids)} fragments checked, {mp_quotes} verified quotes.\n", flush=True)
            fout.flush()


    # Save the speaker-label near-miss audit for optional review.
    with open(audit_file, "w", newline="", encoding="utf-8") as faudit:
        writer = csv.writer(faudit)
        writer.writerow(["search_name", "speaker_label", "near_miss_count"])
        for (search_name, label), count in sorted(audit.items()):
            writer.writerow([search_name, label, count])


# Existing MP list from the Colab notebook; edit this list to change the targets.
MPS_CSV = 'search_name,last_name,chamber,role_notes\n"Sloane, Ms, Kellie",Sloane,LH,"Leader of the Opposition; Leader of the Liberals; Member for Vaucluse"\n"Speakman, Mr, Mark",Speakman,LH,"Shadow Minister for Education; Member for Cronulla"\n"Farlow, Mr, Scott",Farlow,UH,"Shadow Treasurer; Shadow Minister for the Hunter"\n"Tudehope, Mr, Damien",Tudehope,UH,"Shadow Attorney General; Shadow Minister for Youth Justice; Leader of the Opposition in the Legislative Council"\n"Clancy, Mr, Justin",Clancy,LH,"Deputy Leader of the Liberals in the Legislative Assembly; Shadow Minister for Skills, TAFE and Tertiary Education; Shadow Minister for Medical Research"\n"Wallace, Mr, James",Wallace,LH,"Shadow Minister for Industrial Relations; Shadow Minister for Government Procurement"\n"Munro, Ms, Jacqui",Munro,UH,"Shadow Minister for Environment; Shadow Minister for Science and Technology; Shadow Minister for Heritage; Shadow Minister for the Central Coast"\n"Griffin, Mr, James",Griffin,LH,"Shadow Minister for Energy and Climate Change; Shadow Minister for Digital, Artificial Intelligence and Investment; Shadow Minister for Customer Service; Manager of Opposition Business"\n"James, Mr, Tim",James,LH,"Shadow Minister for Fair Trading, Work Health and Safety and Building; Shadow Minister for Local Government; Shadow Minister for Veterans"\n"Coure, Mr, Mark",Coure,LH,"Shadow Minister for Roads; Shadow Minister for Multiculturalism; Shadow Minister for South Western Sydney"\n"Ayyad, Mrs, Tina",Ayyad,LH,"Member for Holsworthy"\n"Davies, Mrs, Tanya",Davies,LH,"Member for Badgerys Creek"\n"Preston, Ms, Robyn",Preston,LH,"Member for Hawkesbury"\n"Tuckerman, Mrs, Wendy",Tuckerman,LH,"Member for Goulburn"\n"Henskens, Mr, Alister",Henskens,LH,"Shadow Special Minister of State; Shadow Minister for Government Accountability; Member for Wahroonga"\n"Ward, Ms, Natalie",Ward,UH,"Deputy Leader of the Liberals; Deputy Leader of the Liberals in the Legislative Council; Shadow Minister for Transport and Infrastructure; Shadow Minister for Sport"\n"Rath, Mr, Chris",Rath,UH,"Shadow Minister for the Illawarra and South Coast; Shadow Minister for Planning and Public Spaces; Shadow Minister for Housing; Shadow Minister for Cities; Shadow Minister for the Arts; Opposition Whip in the Legislative Council"\n"Maclaren-Jones, Ms, Natasha",Maclaren-Jones,UH,"Shadow Minister for Families and Communities; Shadow Minister for Prevention of Domestic Violence and Sexual Assault; Shadow Minister for Disability Inclusion; Shadow Minister for Homelessness; Shadow Minister for Youth"\n"Roberts, Mr, Anthony",Roberts,LH,"Shadow Minister for Police and Counter-terrorism; Shadow Minister for Corrections; Member for Lane Cove"\n"Ajaka, Mr, John",Ajaka,UH,"Former Liberal MLC"'


def prepare_mps_file(path="mps.csv"):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(MPS_CSV + "\n")
    return path

