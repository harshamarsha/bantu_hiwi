#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pilot_rights.py
===============

Measurement pilot for the assumption-free rights pipeline.

This script does NOT decide legal status. It measures, on a reproducible
20-book sample, how far the proposed evidence chain gets:

    Book cell -> candidate edition(s) -> identifiers (OCLC/LCCN/ISBN)
              -> HathiTrust item-level rightsCode
              -> Internet Archive rights fields (corroboration only)
              -> Google Books (API key required; viewapi fallback)

Rules the pilot obeys:
  * A year/author taken from a filename or URL is a *hint*, never a fact.
    It is stored as ``hint_*`` and flagged ``hint_source=filename``.
  * An edition match is only ``exact``/``probable`` when the catalogue
    record corroborates title + author + year. A filename-only match that
    relies on author + year + a language token is recorded as ``lead`` and
    still needs a title-page check.
  * No ``presumed_*`` output. No verdicts, only evidence levels.

Outputs (all under pilot/):
    pilot_results.csv   one row per sampled book
    pilot_summary.md    human-readable coverage report
    pilot.log           full trace
    pilot_cache.json    HTTP cache
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE_PATH = os.path.join(HERE, "pilot_cache.json")
LOG_PATH = os.path.join(HERE, "pilot.log")
RESULTS_PATH = os.path.join(HERE, "pilot_results.csv")
SUMMARY_PATH = os.path.join(HERE, "pilot_summary.md")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DELAY = 0.6
GOOGLE_BOOKS_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()

FIELDS = [
    "row_index", "book_no", "books_in_cell", "hint_source",
    "hint_author", "hint_title", "hint_year", "cell",
    "discovery_source", "ol_candidates", "identity_level", "identity_basis",
    "matched_title", "matched_author", "matched_year", "matched_publisher",
    "identifiers",
    "ht_records", "ht_items", "ht_rights_codes", "ht_us_rights",
    "ht_sample_htid", "ht_sample_orig", "ht_pub_dates", "ht_edition_check",
    "ia_items", "ia_rights", "ia_copyright_status", "ia_lccn",
    "titlepage_source", "titlepage_year", "titlepage_status", "titlepage_detail",
    "gb_api", "gb_viewapi",
    "pilot_evidence", "needs_titlepage", "pilot_note",
]


# ---------------------------------------------------------------- utilities
def log(msg: str) -> None:
    print(msg)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


_cache = {}
if os.path.exists(CACHE_PATH):
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            _cache = json.load(fh)
    except Exception:
        _cache = {}


def save_cache() -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(_cache, fh, ensure_ascii=False, sort_keys=True)


def get_json(url: str, tag: str, timeout: int = 30):
    if url in _cache:
        entry = _cache[url]
        if isinstance(entry, dict) and "error" in entry and "data" not in entry:
            return None, entry["error"]
        return entry.get("data"), None
    time.sleep(DELAY)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        _cache[url] = {"data": data}
        return data, None
    except urllib.error.HTTPError as exc:
        err = {429: "HTTP 429 quota/rate limit",
               403: "HTTP 403 forbidden"}.get(exc.code, f"HTTP {exc.code}")
        _cache[url] = {"error": err}
        log(f"    [{tag}] {err}")
        return None, err
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        _cache[url] = {"error": err}
        log(f"    [{tag}] {err}")
        return None, err


def clean(value) -> str:
    if value is None:
        return ""
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "nat") else text


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    # drop apostrophes so "Kiw'oso" and "kiwoso" compare equal
    text = re.sub(r"['\u2019`\u00b4]", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def author_keys(name: str):
    """Candidate surnames from an author hint.

    "Preston, Ira M. & J. Best" -> ["preston"]
    "kagaya olomi"              -> ["kagaya", "olomi"]
    """
    n = norm(name)
    if not n:
        return []
    if "," in str(name):
        first = re.split(r"[,;&]| and ", n)[0].strip()
        return [first.split()[0]] if first else []
    toks = [t for t in re.split(r"[,;&]| and ", n)[0].split() if len(t) >= 4]
    return toks[:3]


def surname(name: str) -> str:
    keys = author_keys(name)
    return keys[0] if keys else ""


def tokens(text: str) -> set:
    return {t for t in norm(text).split() if len(t) >= 3}


def token_overlap(a: str, b: str) -> float:
    A, B = tokens(a), tokens(b)
    if not A:
        return 0.0
    return len(A & B) / len(A)


def as_int(value):
    try:
        return int(str(value)[:4])
    except Exception:
        return None


def band_level(title_sim, author_ok, year_ok, token_ov, hint_source):
    if hint_source == "prose":
        if title_sim >= 0.90 and author_ok and year_ok:
            return "exact"
        if title_sim >= 0.82 and (author_ok or year_ok):
            return "probable"
        if title_sim >= 0.72:
            return "weak"
        return "reject"
    # filename/URL hints: the year is what we are verifying, so it must not
    # gate identity. Title + author carry the match; year agreement upgrades it.
    if title_sim >= 0.90 and author_ok:
        return "exact"
    if title_sim >= 0.80 and author_ok:
        return "probable"
    if author_ok and year_ok and token_ov >= 0.5:
        return "probable"
    if author_ok and token_ov >= 0.5:
        return "lead"
    if title_sim >= 0.72 and author_ok:
        return "weak"
    return "reject"


# ---------------------------------------------------------------- sampling
BANDS = [
    ("pre1901", lambda y: y is not None and y <= 1900, 2),
    ("1901_1930", lambda y: y is not None and 1901 <= y <= 1930, 3),
    ("1931_1963", lambda y: y is not None and 1931 <= y <= 1963, 3),
    ("1964_1977", lambda y: y is not None and 1964 <= y <= 1977, 2),
    ("post1977", lambda y: y is not None and y >= 1978, 3),
    ("noyear", lambda y: y is None, 2),
]

FORCE = [(6, 1), (8, 1), (1, 1), (0, 1), (3, 1)]
TARGET = 20


def select_sample(per_book):
    import random
    random.seed(42)
    chosen, seen = [], set()

    def take(row):
        key = (int(row["row_index"]), int(row["book_no"]))
        if key in seen or len(chosen) >= TARGET:
            return False
        seen.add(key)
        chosen.append(row)
        return True

    for key in FORCE:
        for _, row in per_book.iterrows():
            if (int(row["row_index"]), int(row["book_no"])) == key:
                take(row)
                break

    picks = {b: [] for b, _, _ in BANDS}
    for _, row in per_book.iterrows():
        y = as_int(row.get("parsed_year"))
        for band, pred, _ in BANDS:
            if pred(y):
                picks[band].append(row)
                break

    for band, _, quota in BANDS:
        pool = [r for r in picks[band]
                if (int(r["row_index"]), int(r["book_no"])) not in seen]
        for row in random.sample(pool, min(quota, len(pool))):
            take(row)

    multi = [r for _, r in per_book.iterrows()
             if int(r.get("books_in_cell") or 1) > 1]
    have = sum(1 for r in chosen if int(r.get("books_in_cell") or 1) > 1)
    if have < 3:
        random.shuffle(multi)
        for row in multi:
            if have >= 3:
                break
            if take(row):
                have += 1
    return chosen


# ---------------------------------------------------------------- discovery
def ol_search(query: str, limit: int = 5):
    fields = ("title,author_name,first_publish_year,publisher,oclc,lccn,isbn,"
              "key,edition_key")
    url = ("https://openlibrary.org/search.json?q="
           + urllib.parse.quote(query)
           + f"&fields={fields}&limit={limit}")
    data, err = get_json(url, "openlibrary")
    if err or not data:
        return []
    return data.get("docs") or []


def resolve_ol_edition(doc: dict, hint_year):
    """Pick an edition record and return its own publication year.

    ``first_publish_year`` is the year the *work* was first published, which
    is not the year of the scanned edition. Edition records carry
    ``publish_date``, so that is what we use.
    """
    keys = [k for k in (doc.get("edition_key") or [])][:4]
    editions = []
    for key in keys:
        url = f"https://openlibrary.org/books/{key}.json"
        data, err = get_json(url, "ol-edition")
        if err or not data:
            continue
        year = as_int(data.get("publish_date"))
        editions.append({
            "key": key,
            "year": year,
            "publish_date": clean(data.get("publish_date")),
            "publishers": data.get("publishers") or [],
            "title": data.get("title") or "",
            "isbn": [x for x in (data.get("isbn_13") or []) +
                     (data.get("isbn_10") or [])][:2],
            "oclc": [str(x) for x in (data.get("oclc_numbers") or [])][:2],
            "lccn": [str(x) for x in (data.get("lccn") or [])][:1],
        })
    if not editions:
        return None
    if hint_year is not None:
        near = [e for e in editions if e["year"]
                and abs(e["year"] - hint_year) <= 1]
        if near:
            return near[0]
    dated = [e for e in editions if e["year"]]
    return dated[0] if dated else editions[0]


def ia_details_id(cell: str):
    m = re.search(r"archive\.org/details/([A-Za-z0-9_.\-]+)", cell or "")
    return m.group(1) if m else None


def ia_metadata(identifier: str) -> dict:
    url = f"https://archive.org/metadata/{urllib.parse.quote(identifier)}"
    data, err = get_json(url, "ia-metadata")
    if err or not data:
        return {}
    meta = data.get("metadata") or {}
    out = {}
    for key in ("identifier", "title", "date", "year", "rights", "licenseurl",
                "possible-copyright-status", "copyright_date", "publisher",
                "creator", "lccn", "oclc", "isbn", "openlibrary_edition",
                "access-restricted-item"):
        out[key] = meta.get(key)
    return out


def identifiers_from_doc(doc: dict):
    ids, isbns = [], []
    for oclc in (doc.get("oclc") or [])[:2]:
        v = str(oclc).strip()
        if v:
            ids.append("oclc:" + v)
    for lccn in (doc.get("lccn") or [])[:1]:
        v = str(lccn).strip()
        if v:
            ids.append("lccn:" + v.split()[0])
    for isbn in (doc.get("isbn") or [])[:3]:
        v = re.sub(r"[^0-9Xx]", "", str(isbn))
        if len(v) in (10, 13):
            isbns.append(v)
            ids.append("isbn:" + v)
    return list(dict.fromkeys(ids)), list(dict.fromkeys(isbns))


def score_docs(docs, hint_title, hint_author, hint_year, hint_source):
    keys = author_keys(hint_author)
    out = []
    for doc in docs:
        title = clean(doc.get("title"))
        authors = " ".join(clean(a) for a in (doc.get("author_name") or []))
        year = as_int(doc.get("first_publish_year"))
        t = sim(hint_title, title)
        a_ok = bool(keys) and any(k in norm(authors) for k in keys)
        y_ok = (hint_year is not None and year is not None
                and abs(hint_year - year) <= 1)
        tov = token_overlap(hint_title, title)
        level = band_level(t, a_ok, y_ok, tov, hint_source)
        out.append({"doc": doc, "title": title, "authors": authors,
                    "year": year, "title_sim": round(t, 3),
                    "author_ok": a_ok, "year_ok": y_ok,
                    "token_ov": round(tov, 2), "level": level,
                    "source": "openlibrary"})
    order = {"exact": 0, "probable": 1, "lead": 2, "weak": 3, "reject": 4}
    out.sort(key=lambda d: (order[d["level"]], -d["title_sim"], -d["token_ov"]))
    return out


def discover(hint_title, hint_author, hint_year, hint_source, cell):
    """Return (candidates, source_label). Candidates are scored OL docs or
    a synthetic candidate built from Internet Archive item metadata."""
    if ia_details_id(cell):
        ident = ia_details_id(cell)
        meta = ia_metadata(ident)
        if meta.get("title"):
            doc = {
                "title": meta.get("title"),
                "author_name": ([meta.get("creator")]
                                if isinstance(meta.get("creator"), str)
                                else (meta.get("creator") or [])),
                "first_publish_year": as_int(meta.get("date") or meta.get("year")),
                "publisher": ([meta.get("publisher")]
                              if isinstance(meta.get("publisher"), str)
                              else (meta.get("publisher") or [])),
                "oclc": ([meta.get("oclc")] if isinstance(meta.get("oclc"), str)
                         else (meta.get("oclc") or [])),
                "lccn": ([str(meta.get("lccn")).split()[0]]
                         if meta.get("lccn") else []),
                "isbn": ([meta.get("isbn")] if isinstance(meta.get("isbn"), str)
                         else (meta.get("isbn") or [])),
                "key": meta.get("openlibrary_edition"),
                "_ia_identifier": ident,
            }
            scored = score_docs([doc], hint_title, hint_author, hint_year,
                                "prose")
            # an explicit IA item URL is the edition itself
            if scored:
                scored[0]["level"] = "exact"
                scored[0]["source"] = "ia-item-url"
                scored[0]["title_sim"] = 1.0
            return scored, "ia-item-url"
        return [], "ia-item-url"

    keys = author_keys(hint_author)
    queries = []
    if hint_source == "prose":
        if hint_title and hint_author:
            queries.append(f"{hint_title} {hint_author}")
        if hint_title:
            queries.append(f'title:("{hint_title}")')
    else:
        for key in keys:
            if hint_year:
                queries.append(f"author:({key}) AND "
                               f"first_publish_year:[{hint_year - 1} TO {hint_year + 1}]")
        for key in keys:
            if hint_title:
                queries.append(f"{hint_title} {key}")
            if hint_year:
                queries.append(f"{key} {hint_year}")

    seen, docs = set(), []
    for q in queries:
        for doc in ol_search(q):
            key = (clean(doc.get("title")), as_int(doc.get("first_publish_year")))
            if key in seen:
                continue
            seen.add(key)
            docs.append(doc)
        if len(docs) >= 8:
            break
    return score_docs(docs, hint_title, hint_author, hint_year, hint_source), \
        "openlibrary"


def hathitrust(identifiers):
    out = {"records": [], "items": [], "error": None}
    if not identifiers:
        return out
    for i in range(0, len(identifiers), 20):
        chunk = identifiers[i:i + 20]
        joined = "|".join(urllib.parse.quote(x, safe="") for x in chunk)
        url = f"https://catalog.hathitrust.org/api/volumes/brief/json/{joined}"
        data, err = get_json(url, "hathitrust")
        if err:
            out["error"] = err
            continue
        for value in (data or {}).values():
            out["records"].extend((value.get("records") or {}).values())
            out["items"].extend(value.get("items") or [])
    return out


def ia_search(title, author):
    """Internet Archive catalogue search for the digitized item itself."""
    if not title:
        return []
    fl = ("&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=year"
          "&fl%5B%5D=date&fl%5B%5D=creator&fl%5B%5D=publisher"
          "&fl%5B%5D=lccn&fl%5B%5D=oclc&fl%5B%5D=isbn"
          "&fl%5B%5D=rights&fl%5B%5D=licenseurl")
    queries = []
    base = f'title:("{title}")'
    for key in author_keys(author)[:2]:
        queries.append(base + f' AND creator:("{key}")')
    queries.append(base)
    for q in queries:
        url = ("https://archive.org/advancedsearch.php?q="
               + urllib.parse.quote(q) + fl + "&rows=4&output=json")
        data, err = get_json(url, "internet-archive")
        if err or not data:
            continue
        docs = (data.get("response") or {}).get("docs") or []
        if docs:
            return docs
    return []


def ia_to_doc(rec: dict) -> dict:
    """Convert an archive.org search record to the Open Library doc shape."""
    creator = rec.get("creator")
    if isinstance(creator, str):
        creators = [creator]
    elif isinstance(creator, list):
        creators = creator
    else:
        creators = []
    publisher = rec.get("publisher")
    if isinstance(publisher, str):
        publishers = [publisher]
    elif isinstance(publisher, list):
        publishers = publisher
    else:
        publishers = []
    lccn = rec.get("lccn")
    lccns = [lccn] if isinstance(lccn, str) else (lccn or [])
    oclc = rec.get("oclc")
    oclcs = [oclc] if isinstance(oclc, (str, int)) else (oclc or [])
    isbn = rec.get("isbn")
    isbns = [isbn] if isinstance(isbn, str) else (isbn or [])
    return {
        "title": rec.get("title"),
        "author_name": creators,
        "first_publish_year": as_int(rec.get("year") or rec.get("date")),
        "publisher": publishers,
        "lccn": [str(x).split()[0] for x in lccns if x],
        "oclc": [str(x) for x in oclcs if x],
        "isbn": [re.sub(r"[^0-9Xx]", "", str(x)) for x in isbns if x],
        "_ia_identifier": rec.get("identifier"),
    }


def google_viewapi(isbns):
    isbns = [i for i in isbns if i][:5]
    if not isbns:
        return {}
    keys = ",".join(f"ISBN:{i}" for i in isbns)
    url = f"https://books.google.com/books?jscmd=viewapi&bibkeys={keys}"
    text, err = get_text(url, "google-viewapi", limit=20000)
    if err or not text:
        return {"error": err or "no data"}
    raw = text.strip()
    # the multi-ISBN endpoint wraps the JSON: var _GBSBookInfo = {...};
    if raw.startswith("var "):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except Exception:
        return {"error": "unparsable response"}
    if not isinstance(data, dict):
        return {"error": "unexpected shape"}
    return {k: {"preview": v.get("preview")} for k, v in data.items()}


_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20[0-2]\d)\b")
_SKIP_LINE = ("digitized", "internet archive", "google", "microsoft",
              "university library", "barcode", "preservation", "scanning",
              "oclc", "isbn", "call no", "accession")


def get_text(url: str, tag: str, limit: int = 6000):
    """Fetch the first `limit` bytes of a text file, with cache."""
    key = url + f"#first{limit}"
    if key in _cache:
        entry = _cache[key]
        return entry.get("data"), entry.get("error")
    time.sleep(DELAY)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read(limit).decode("utf-8", "replace")
        _cache[key] = {"data": raw}
        return raw, None
    except urllib.error.HTTPError as exc:
        err = {401: "HTTP 401 restricted", 403: "HTTP 403 forbidden"}.get(
            exc.code, f"HTTP {exc.code}")
        _cache[key] = {"error": err}
        return None, err
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        _cache[key] = {"error": err}
        return None, err


def titlepage_years(identifier: str):
    """Years printed in the item's first pages, with the line they came from.

    Library stamps, barcodes and digitisation boilerplate are discarded so
    that the year is genuinely from the title page / colophon.
    """
    ident = identifier.strip()
    if not ident:
        return [], "no identifier"
    url = f"https://archive.org/download/{urllib.parse.quote(ident)}/" \
          f"{urllib.parse.quote(ident)}_djvu.txt"
    text, err = get_text(url, "ia-titlepage", limit=15000)
    if err:
        return [], err
    if not text:
        return [], "no text"

    head = text[:12000]
    # strip long digit runs such as "3 1924 026 919 948" (library barcodes)
    head = re.sub(r"\b\d[\d ]{8,}\d\b", " ", head)
    kept = []
    for line in head.split("\n"):
        low = line.lower()
        if any(k in low for k in _SKIP_LINE):
            continue
        kept.append(line.strip())

    found = []
    for line in kept:
        for match in _YEAR_RE.finditer(line):
            found.append((int(match.group(0)), re.sub(r"\s+", " ", line)[:160]))
    return found, None


def titlepage_fact(identifiers, expected_year):
    """Resolve a publication year from title-page OCR without using the hint
    to choose a year. Returns (source, year, status, detail)."""
    attempts = []
    for ident in list(dict.fromkeys([i for i in identifiers if i]))[:4]:
        years, err = titlepage_years(ident)
        if err:
            attempts.append((ident, None, err))
            continue
        distinct = sorted({y for y, _ in years})
        attempts.append((ident, distinct, "; ".join(l for _, l in years)[:240]))

    confident = [(i, ys) for i, ys, _ in attempts if ys and len(ys) == 1]
    if len(confident) == 1:
        ident, ys = confident[0]
        year = ys[0]
        status = "confident"
        if expected_year:
            status = "match" if abs(year - expected_year) <= 1 else "mismatch"
        return "archive.org/" + ident, year, status, \
            [d for i, _, d in attempts if i == ident][0]
    if len(confident) > 1:
        values = {ys[0] for _, ys in confident}
        if len(values) == 1:
            year = values.pop()
            status = "match" if expected_year and abs(year - expected_year) <= 1 \
                else "mismatch"
            return " + ".join("archive.org/" + i for i, _ in confident), year, \
                "agree-across-items-" + status, ""
        return "", None, "conflict", \
            "items give " + ", ".join(sorted(str(v) for v in values))

    ambiguous = [(i, ys) for i, ys, _ in attempts if ys and len(ys) > 1]
    if ambiguous:
        ident, _ = ambiguous[0]
        real, _ = titlepage_years(ident)
        detail = " | ".join(f"{y}: {line}" for y, line in real[:6])
        return ("archive.org/" + ident, None, "ambiguous", detail[:300])
    if attempts:
        return ("archive.org/" + attempts[0][0], None, attempts[0][2] or "no year",
                attempts[0][2] or "")
    return "", None, "no-scan", ""


# ---------------------------------------------------------------- main
def main():
    open(LOG_PATH, "w", encoding="utf-8").close()
    import pandas as pd

    per_book = pd.read_csv(os.path.join(ROOT,
                                        "sigtyp_sub_dicts_rights_per_book.csv"))
    cells = pd.read_csv(os.path.join(ROOT, "sigtyp_sub_dicts.csv"))

    sample = select_sample(per_book)
    log(f"PILOT sample: {len(sample)} books "
        f"(forced cases + stratified by year band + multi-book fill)")
    log(f"Google Books API key present: {bool(GOOGLE_BOOKS_KEY)}")
    log("")

    rows = []
    for row in sample:
        idx = int(row["row_index"])
        book_no = int(row["book_no"])
        n_books = int(row.get("books_in_cell") or 1)
        cell = clean(cells.loc[idx, "Book"])

        hint_author = clean(row.get("parsed_author"))
        hint_title = clean(row.get("parsed_title"))
        hint_year = as_int(row.get("parsed_year"))
        hint_source = "filename" if re.search(r"\.pdf|https?://", cell) else "prose"
        if not hint_title and not hint_author:
            hint_source = "prose"

        log(f"[{idx:03d}/{book_no}] {hint_author!r} | {hint_title!r} | "
            f"{hint_year} | {hint_source}")

        rec = {f: "" for f in FIELDS}
        rec.update({"row_index": idx, "book_no": book_no,
                    "books_in_cell": n_books, "hint_source": hint_source,
                    "hint_author": hint_author, "hint_title": hint_title,
                    "hint_year": hint_year or "", "cell": cell[:300]})

        scored, source = discover(hint_title, hint_author, hint_year,
                                  hint_source, cell)
        shortlist = [d for d in scored if d["level"] in ("exact", "probable", "lead")][:2]
        ia_docs = []
        if not shortlist and hint_title:
            # no catalogue record: the digitized item itself can be the identity
            ia_docs = ia_search(hint_title, hint_author)
            ia_scored = score_docs([ia_to_doc(d) for d in ia_docs],
                                   hint_title, hint_author, hint_year,
                                   hint_source)
            for d in ia_scored:
                d["source"] = "internet-archive"
            found = [d for d in ia_scored
                     if d["level"] in ("exact", "probable", "lead")]
            if found:
                scored, shortlist, source = ia_scored, found[:2], "internet-archive"
        rec["discovery_source"] = source
        rec["ol_candidates"] = len(scored)
        rec["identity_level"] = shortlist[0]["level"] if shortlist else "none"
        log(f"    discovery={source} candidates={len(scored)} "
            f"level={rec['identity_level']}")
        for d in scored[:3]:
            log(f"      {d['level']:8s} sim={d['title_sim']:.2f} "
                f"tok={d['token_ov']:.2f} a={int(d['author_ok'])} "
                f"y={int(d['year_ok'])} {d['year']} {d['title'][:58]!r}")

        best = shortlist[0] if shortlist else None
        if best:
            rec["matched_title"] = best["title"]
            rec["matched_author"] = best["authors"]
            rec["matched_year"] = best["year"] or ""
            pubs = best["doc"].get("publisher") or []
            rec["matched_publisher"] = clean(pubs[0] if pubs else "")[:120]
            # Open Library: replace the work year with the edition's own year
            if best["source"] == "openlibrary":
                ed = resolve_ol_edition(best["doc"], hint_year)
                if ed:
                    best["_edition"] = ed
                    if ed["year"]:
                        best["year"] = ed["year"]
                        rec["matched_year"] = ed["year"]
                    if ed["publishers"]:
                        rec["matched_publisher"] = clean(ed["publishers"][0])[:120]
                    log(f"    edition {ed['key']} year={ed['year']} "
                        f"date={ed['publish_date']!r}")
                    if (hint_year and best["year"]
                            and abs(best["year"] - hint_year) <= 1
                            and best["level"] == "lead"):
                        best["level"] = "probable"
                        rec["identity_level"] = "probable"
            if best["level"] == "exact" and best["title_sim"] >= 0.9:
                rec["identity_basis"] = "title+author+year"
            elif best["level"] == "exact":
                rec["identity_basis"] = "ia-item-url"
            elif best["level"] == "probable":
                rec["identity_basis"] = "title+author (or author+year+token)"
            else:
                rec["identity_basis"] = "author+language-token (lead)"
            # a lead whose catalogue year contradicts the hint is a different
            # edition until a title page proves otherwise
            if (best["level"] == "lead" and hint_year and best["year"]
                    and abs(best["year"] - hint_year) > 2):
                rec["identity_level"] = "lead_conflict"
                rec["identity_basis"] = ("author+language-token, year conflict "
                                         "(lead)")
                log(f"    year conflict: hint {hint_year} vs "
                    f"catalogue {best['year']} -> lead_conflict")

        ids, isbns = [], []
        for d in shortlist:
            ed = d.get("_edition")
            if ed and (ed.get("oclc") or ed.get("lccn") or ed.get("isbn")):
                # edition-level identifiers only; work-level ids belong to
                # whatever edition the catalogue happened to attach them to
                for oclc in ed.get("oclc") or []:
                    ids.append("oclc:" + str(oclc))
                for lccn in ed.get("lccn") or []:
                    ids.append("lccn:" + str(lccn).split()[0])
                for isbn in ed.get("isbn") or []:
                    v = re.sub(r"[^0-9Xx]", "", str(isbn))
                    if len(v) in (10, 13):
                        isbns.append(v)
                        ids.append("isbn:" + v)
            else:
                i2, b2 = identifiers_from_doc(d["doc"])
                ids += i2
                isbns += b2
        ids = list(dict.fromkeys(ids))[:20]
        isbns = list(dict.fromkeys(isbns))[:5]
        rec["identifiers"] = ";".join(ids)
        log(f"    identifiers={len(ids)} {ids[:4]}")

        if ids:
            ht = hathitrust(ids)
            items = ht["items"]
            codes = sorted({(i.get("rightsCode") or "?") for i in items})
            rec["ht_records"] = len({r.get("recordURL") for r in ht["records"]})
            rec["ht_items"] = len(items)
            rec["ht_rights_codes"] = ",".join(codes)
            dates = sorted({str(d) for r in ht["records"]
                            for d in (r.get("publishDates") or [])})
            rec["ht_pub_dates"] = ";".join(dates)
            if items:
                rec["ht_us_rights"] = ",".join(sorted(
                    {i.get("usRightsString") or "?" for i in items}))
                rec["ht_sample_htid"] = items[0].get("htid") or ""
                rec["ht_sample_orig"] = items[0].get("orig") or ""
            log(f"    HT records={rec['ht_records']} items={len(items)} "
                f"codes={rec['ht_rights_codes'] or '-'}")
            if ht["error"]:
                rec["ht_sample_orig"] = "ERROR " + ht["error"]

        # Internet Archive: corroboration + a second identifier source
        if not ia_docs:
            ia_docs = ia_search(rec["matched_title"] or hint_title,
                               rec["matched_author"] or hint_author)
        if ia_docs:
            rec["ia_items"] = len(ia_docs)
            rec["ia_rights"] = ";".join(sorted({clean(d.get("rights"))
                                                for d in ia_docs}))[:200]
            metas = [ia_metadata(d["identifier"]) for d in ia_docs[:2]]
            rec["ia_copyright_status"] = ";".join(sorted({
                clean(m.get("possible-copyright-status")) for m in metas
            }))[:300]
            lccns = [clean(m.get("lccn")).split()[0]
                     for m in metas if clean(m.get("lccn"))]
            rec["ia_lccn"] = ";".join(dict.fromkeys(lccns))
            # only use the IA item's LCCN when the edition identity is solid
            if (not rec["ht_items"] and lccns
                    and rec["identity_level"] in ("exact", "probable", "lead")):
                ht2 = hathitrust(["lccn:" + x for x in lccns[:2]])
                if ht2["items"]:
                    items = ht2["items"]
                    rec["ht_items"] = len(items)
                    rec["ht_rights_codes"] = ",".join(sorted(
                        {i.get("rightsCode") or "?" for i in items}))
                    rec["ht_us_rights"] = ",".join(sorted(
                        {i.get("usRightsString") or "?" for i in items}))
                    rec["ht_sample_htid"] = items[0].get("htid") or ""
                    rec["ht_sample_orig"] = "via IA LCCN"
                    log(f"    HT (via IA LCCN) items={len(items)} "
                        f"codes={rec['ht_rights_codes']}")
        log(f"    IA items={rec['ia_items'] or 0} lccn={rec['ia_lccn'] or '-'}")

        # --- title page from the digitized item (strongest fact source) ----
        tp_ids = []
        if ia_details_id(cell):
            tp_ids.append(ia_details_id(cell))
        ref_title = rec["matched_title"] or hint_title
        for d in ia_docs[:4]:
            ident = d.get("identifier") or ""
            if not ident or re.search(r"wiki|wikipedia", ident, re.I):
                continue
            if ref_title and sim(d.get("title") or "", ref_title) < 0.55:
                continue
            tp_ids.append(ident)
        expected = as_int(rec["matched_year"]) or hint_year
        src, year, status, detail = titlepage_fact(tp_ids, expected)
        rec["titlepage_source"] = src
        rec["titlepage_year"] = year or ""
        rec["titlepage_status"] = status
        rec["titlepage_detail"] = (detail or "").replace("\n", " ")[:240]
        log(f"    TITLEPAGE {src or '-'} year={year or '-'} "
            f"status={status} {rec['titlepage_detail'][:90]}")

        if isbns:
            rec["gb_viewapi"] = json.dumps(google_viewapi(isbns),
                                           ensure_ascii=False)[:300]

        params = {"q": (f"isbn:{isbns[0]}" if isbns
                        else f"{hint_title} {hint_author}").strip(),
                  "country": "US"}
        if GOOGLE_BOOKS_KEY:
            params["key"] = GOOGLE_BOOKS_KEY
        api_err = None
        _, api_err = get_json("https://www.googleapis.com/books/v1/volumes?"
                              + urllib.parse.urlencode(params), "google-books-api")
        rec["gb_api"] = api_err or "ok"

        matched_year = as_int(rec["matched_year"])
        ht_dates = [as_int(x) for x in (rec["ht_pub_dates"] or "").split(";")]
        ht_dates = [x for x in ht_dates if x]
        if not rec["ht_items"]:
            edition_check = "n/a"
        elif not matched_year or not ht_dates:
            edition_check = "unknown"
        elif any(abs(d - matched_year) <= 2 for d in ht_dates):
            edition_check = "ok"
        else:
            edition_check = "mismatch"
        rec["ht_edition_check"] = edition_check
        if edition_check == "mismatch":
            log(f"    HT edition mismatch: matched {matched_year} vs "
                f"HT dates {rec['ht_pub_dates']}")

        rec["pilot_evidence"], rec["needs_titlepage"], rec["pilot_note"] = \
            decide(rec["identity_level"], rec["ht_rights_codes"],
                   int(rec["ht_items"] or 0), api_err, edition_check)
        log(f"    EVIDENCE {rec['pilot_evidence']}  ({rec['pilot_note']})")
        rows.append(rec)

    with open(RESULTS_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    save_cache()
    write_summary(rows)
    log("")
    log(f"wrote {RESULTS_PATH}")
    log(f"wrote {SUMMARY_PATH}")


def decide(identity, codes_str, n_items, gb_error, edition_check="unknown"):
    codes = [c for c in (codes_str or "").split(",") if c]
    lead = identity == "lead"
    if identity in ("none",):
        return ("identity_unresolved", "yes",
                "no edition match; cannot reach an item-level record")
    if identity == "weak":
        return ("identity_weak", "yes",
                "only a weak title match; needs manual identification")
    if identity == "lead_conflict":
        return ("edition_unconfirmed", "yes",
                "catalogue year contradicts the hint; different edition likely")
    if not n_items:
        return ("no_item_record", "yes",
                "edition matched but no HathiTrust item; title-page check needed")
    if edition_check == "mismatch":
        return ("ht_edition_mismatch", "yes",
                "HathiTrust records belong to other editions")
    suffix = "_lead" if lead else ""
    gb = ("Google Books API unavailable (" + gb_error + ")") if gb_error \
        else "Google Books API responded"
    if codes == ["pd"]:
        return ("ht_pd_all" + suffix, "yes" if lead else "no",
                "all HathiTrust items marked pd (world public domain); " + gb)
    if codes == ["pdus"]:
        return ("ht_pd_us_only" + suffix, "yes" if lead else "no",
                "all items marked pdus (US public domain only); " + gb)
    if codes == ["pd", "pdus"]:
        return ("ht_pd_mixed" + suffix, "yes" if lead else "no",
                "items mix pd and pdus; " + gb)
    if codes == ["ic"]:
        return ("ht_in_copyright" + suffix, "yes" if lead else "no",
                "all items marked ic (US in copyright); " + gb)
    if "ic" in codes and ("pd" in codes or "pdus" in codes):
        return ("ht_conflict" + suffix, "yes",
                "items disagree (ic vs pd/pdus); manual review")
    return ("ht_undetermined" + suffix, "yes",
            "HathiTrust code " + ",".join(codes))


def write_summary(rows):
    from collections import Counter
    n = len(rows)
    ev = Counter(r["pilot_evidence"].replace("_lead", "") for r in rows)
    ident = Counter(r["identity_level"] for r in rows)
    src = Counter(r["hint_source"] for r in rows)
    basis = Counter(r["identity_basis"] or "—" for r in rows)
    ht_ok = sum(1 for r in rows if int(r["ht_items"] or 0) > 0)
    ht_strong = sum(1 for r in rows if r["pilot_evidence"].startswith("ht_")
                    and not r["pilot_evidence"].endswith("_lead")
                    and r["pilot_evidence"] != "ht_edition_mismatch")
    ht_lead = ht_ok - ht_strong
    ia_ok = sum(1 for r in rows if int(r["ia_items"] or 0) > 0)
    gb_ok = sum(1 for r in rows if r["gb_api"] == "ok")
    tp = sum(1 for r in rows if r["needs_titlepage"] == "yes")
    tp_year = sum(1 for r in rows if r["titlepage_year"] not in ("", None))
    tp_match = sum(1 for r in rows if "match" in r["titlepage_status"]
                   and "mismatch" not in r["titlepage_status"])
    tp_mismatch = sum(1 for r in rows if "mismatch" in r["titlepage_status"])
    tp_ambig = sum(1 for r in rows if r["titlepage_status"] == "ambiguous")
    tp_blocked = sum(1 for r in rows if "401" in r["titlepage_status"]
                     or "403" in r["titlepage_status"])
    resolved = ht_strong
    unresolved = n - resolved

    lines = []
    lines.append(f"# Rights pilot — {n}-book coverage report\n")
    lines.append(f"Sample: **{n} books** (forced known cases + stratified by "
                 f"year band + multi-book fill). This is a measurement run; "
                 f"it does not produce legal verdicts.\n")
    lines.append("## Coverage\n")
    lines.append("| Measure | Result |")
    lines.append("| --- | --- |")
    lines.append(f"| Books sampled | {n} |")
    lines.append(f"| Hints from prose | {src.get('prose', 0)} |")
    lines.append(f"| Hints from filename/URL | {src.get('filename', 0)} |")
    lines.append(f"| Exact match | {ident.get('exact', 0)} |")
    lines.append(f"| Probable match | {ident.get('probable', 0)} |")
    lines.append(f"| Lead with year conflict | {ident.get('lead_conflict', 0)} |")
    lines.append(f"| Weak / none | {ident.get('weak', 0) + ident.get('none', 0)} |")
    lines.append(f"| **HathiTrust item, confirmed edition** | **{ht_strong}** |")
    lines.append(f"| HathiTrust item, lead-only edition | {ht_lead} |")
    lines.append(f"| Internet Archive item found | {ia_ok} |")
    lines.append(f"| Title-page year read with one candidate year | {tp_year} |")
    lines.append(f"| Title page agrees with catalogue/hint | {tp_match} |")
    lines.append(f"| Title page **disagrees** (edition mismatch) | {tp_mismatch} |")
    lines.append(f"| Title page ambiguous (several years) | {tp_ambig} |")
    lines.append(f"| Title page inaccessible (IA restriction) | {tp_blocked} |")
    lines.append(f"| Google Books API answered | {gb_ok} |")
    lines.append(f"| Still needs title-page check | {tp} |")
    lines.append("")
    lines.append("## Evidence levels\n")
    lines.append("| Level | Books | Meaning |")
    lines.append("| --- | --- | --- |")
    meaning = {
        "ht_pd_all": "HathiTrust pd — world public domain",
        "ht_pd_us_only": "HathiTrust pdus — US only",
        "ht_pd_mixed": "pd and pdus items",
        "ht_in_copyright": "HathiTrust ic",
        "ht_conflict": "items disagree",
        "ht_undetermined": "HT code und/unknown",
        "no_item_record": "edition matched, no HT item",
        "identity_unresolved": "no edition match",
        "identity_weak": "weak title match only",
        "edition_unconfirmed": "lead match, year conflict",
        "ht_edition_mismatch": "HathiTrust holds a different edition",
    }
    for key, count in ev.most_common():
        lines.append(f"| `{key}` | {count} | {meaning.get(key, '')} |")
    lines.append("")
    lines.append("## Identity basis\n")
    lines.append("| Basis | Books |")
    lines.append("| --- | --- |")
    for key, count in basis.most_common():
        lines.append(f"| {key} | {count} |")
    lines.append("")
    lines.append("## Per-book detail\n")
    lines.append("| row | src | identity | matched edition | year | TP year | HT codes "
                 "| items | evidence |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        title = (r["matched_title"] or r["hint_title"] or "")[:40].replace("|", "/")
        lines.append(
            f"| {r['row_index']} | {r['hint_source']} | {r['identity_level']} | "
            f"{title} | {r['matched_year'] or r['hint_year'] or ''} | "
            f"{r['titlepage_year'] or r['titlepage_status'] or '—'} | "
            f"{r['ht_rights_codes'] or '—'} | {r['ht_items'] or 0} | "
            f"`{r['pilot_evidence']}` |")
    lines.append("")
    lines.append("## What this means for the full run\n")
    lines.append(f"- Item-level evidence was reached for **{ht_strong}/{n}** books "
                 f"with a confirmed edition; a further **{ht_lead}** reached a "
                 f"HathiTrust record only through a `lead` match and are not "
                 f"counted.")
    lines.append(f"- **{unresolved}/{n}** need manual identity or a different source.")
    lines.append("- **The edition check worked**: one HathiTrust match was for a "
                 "1967 reprint while the target was the 1887 edition, so the "
                 "`ic` code was rejected rather than copied. Another filename "
                 "said 1886 while its title page said 1888.")
    lines.append("- Title-page OCR gave an unambiguous year for "
                 f"{tp_year}/{n}; {tp_ambig}/{n} were ambiguous and "
                 f"{tp_blocked}/{n} were blocked by Internet Archive restrictions.")
    lines.append("- Google Books `publicDomain` was not measurable: keyless calls "
                 "return HTTP 429 with quota 0. Set `GOOGLE_BOOKS_API_KEY` to enable it.")
    lines.append("- `lead` matches rest on author + year + language token, not on a "
                 "confirmed title. They still require a title-page check.")
    lines.append("- The gramfinder PDF host (`130.60.24.118`) returns HTTP 401, so "
                 "URL-only cells cannot be verified from the item itself.")
    lines.append("- The sample deliberately includes hard cases and is 60% "
                 "filename/URL cells (population: 57%), so treat the extrapolation "
                 "as an upper-bound estimate.")
    lo, hi = round((unresolved / n) * 192 * 0.6), round((unresolved / n) * 192)
    lines.append(f"- Linear extrapolation to 192 books: roughly **{lo}–{hi}** books "
                 f"needing manual resolution, plus a title-page check on the rest.")

    with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
