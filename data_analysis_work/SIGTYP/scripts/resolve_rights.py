#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resolve_rights.py
=================

Publication-rights resolution for ``sigtyp_sub_dicts.csv`` using the pilot's
evidence chain, extended with a working Google Books key and more lookup
services so that fewer books need a human.

For every book in every ``Book`` cell:

  1. PARSE the cell (citation, PDF link, archive.org item, bare file name).
     A year taken from a file name or URL is a *hint*, never a fact.
  2. IDENTIFY the edition in several independent catalogues at once:
        Google Books  -- edition date, publicDomain flag, full-view state
        Open Library  -- edition record (publish_date, not first_publish_year)
        Internet Archive -- digitised item metadata
        BnF SRU       -- French national catalogue (title, date, life dates)
        Crossref      -- modern works only
     Google Books free-text ranking is weak, so each book gets a short ladder
     of author+language+kind queries (kind taken from the cell/file name when
     present).  Candidates are scored locally on title / author / year /
     language, never trusted in API order.
  3. CORROBORATE: two independent catalogues that agree on title and year make
     the edition ``corroborated``; one catalogue makes it ``single_source``.
  4. IDENTIFIERS (OCLC / LCCN / ISBN collected above) -> HathiTrust Bib API.
     ``rightsCode`` is used only when the HathiTrust record's ``publishDates``
     match the edition year (a 1967 reprint is not the 1887 edition).
  5. AUTHOR DEATH: VIAF -> English Wikipedia -> Wikidata -> BnF life dates.
  6. VERDICT: statutory US term (95 years) and EU term (life+70), reconciled
     with service evidence (HathiTrust pd/pdus/ic, Google Books publicDomain +
     full view).  A service flag never overrules a different edition.
  7. REVIEW FLAG: ``needs_review=yes`` only when something genuinely cannot be
     decided (no year, contradictory evidence).  Everything else is answered
     by a lookup service or by the statutory rule, with the basis recorded.

Outputs (new files; existing CSVs are never overwritten), all relative to the
repository folder that contains this ``scripts/`` directory:

    sigtyp_sub_dicts_with_rights_v2.csv        input table + evidence columns
    sigtyp_sub_dicts_rights_per_book_v2.csv    one line per book in a cell
    rights_summary_v2.md                       readable report
    logs/rights_lookup_v2.log                  full trace
    logs/rights_cache_v2.json                  HTTP cache (re-runs are cheap)
    backups/<timestamp>/                       previous outputs

Usage (from anywhere; paths are resolved from the script location):
    python3 scripts/resolve_rights.py                     # full run
    python3 scripts/resolve_rights.py --limit 20          # first 20 rows
    python3 scripts/resolve_rights.py --rows 8,9,10       # selected rows
    python3 scripts/resolve_rights.py --offline           # cache only
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from lookup_rights import (
    Http,
    Trace,
    ParsedBook,
    Candidate,
    Providers,
    ProviderUnavailable,
    parse_cell_all,
    norm,
    clean,
    short_title,
    person_name_from,
    us_rights,
    eu_rights,
    overall,
    _plausible_death,
    YEAR_RE,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent          # .../SIGTYP/scripts
ROOT = HERE.parent                              # .../SIGTYP
INPUT_CSV = ROOT / "sigtyp_sub_dicts.csv"
OUTPUT_CSV = ROOT / "sigtyp_sub_dicts_with_rights_v2.csv"
PER_BOOK_CSV = ROOT / "sigtyp_sub_dicts_rights_per_book_v2.csv"
LOG_FILE = ROOT / "logs" / "rights_lookup_v2.log"
CACHE_FILE = ROOT / "logs" / "rights_cache_v2.json"
SUMMARY_FILE = ROOT / "rights_summary_v2.md"
BACKUP_DIR = ROOT / "backups"

BOOK_COLUMN = "Book"
AS_OF_YEAR = 2026

# where the Google Books key may live (first readable one wins)
KEY_PATHS = [
    ROOT / "api_keys" / "google_books_api_key.txt",
    ROOT / "google_books_api_key.txt",          # pre-reorganisation location
    ROOT.parent.parent / "digitizing" / "google_api_key.txt",
]

UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

GB_RESULTS = 40            # metadata only; does not change quota cost
GB_BUDGET = 700            # requests per run, guards the free daily quota
REQUEST_TIMEOUT = 10       # seconds; archive.org and VIAF can stall otherwise
MAX_WORKERS = 4
ROW_WORKERS = 3

KIND_WORDS = ("dictionnaire", "dictionary", "lexique", "vocabulary",
              "dicionario", "diccionario", "worterbuch", "worïterbuch",
              "glossaire", "vocabulaire", "grammaire", "grammar")

DICT_RE = re.compile(
    r"diction|vocab|lexi|w[oö]rterbuch|glossar|diccion|grammar|grammaire|"
    r"grammatica|phrase[- ]?book", re.IGNORECASE)

# print-on-demand and facsimile-reprint houses; a catalogue record from one of
# these is a modern reissue of an old work, not the edition the cell cites
POD_PUBLISHER_RE = re.compile(
    r"nabu|kessinger|biblio\s*l?ife|biblio\s*bazaar|bibleo|ulan|general books|"
    r"forgotten books|createspace|create space|andesite|leopold classic|"
    r"hardpress|hard press|sagwan|palala|wentworth|hassell street|"
    r"creative media partners|legare street|lightning source|"
    r"book on demand|alpha edition|blurb|lulu|\breprint|"
    r"facsimile|lincom", re.IGNORECASE)

# A catalogue record for a marked edition mismatch is treated as a
# reprint (no review flag, verdict stays on the cell year).  Everything in the
# module is automatic, which is why the few finds that needed hand-verification
# live here, keyed by (row_index, book_no).  Each entry is exactly one lookup
# made during the v2 run and documented in RIGHTS_METHOD.md.
EDITION_VERIFICATIONS: dict[tuple[int, int], dict] = {
    # Nomaande lexicon without a year in the cell: SIL Cameroon's archive
    # catalogues the work as published 2003 (compilers: Comité de langue
    # nɔmaándɛ́, Terri R. Scruggs, Carolyn Taylor; 23 pp.).
    (3, 1): {
        "edition_year": 2003,
        "source": "sil_cameroon",
        "title": "Lexique nɔmaándɛ́ - français",
        "note": "SIL Cameroon Language & Culture Archives entry 47934 dates "
                "this lexicon to 2003; the cell gives no year",
    },
    # Stewart 1956 'Xosa phrase book': Google Books catalogues the claimed 1956
    # edition (rev. by B. B. Mdledle); the Open Library 1906 record is an
    # earlier edition of the same work.
    (142, 5): {
        "edition_year": 1956,
        "source": "google_books",
        "title": "Xhosa Phrase Book: (formerly Known as the \"Kafir Phrase "
                 "Book.\") Rev. and Transposed Into the Standard Xhosa Spelling "
                 "by B. B. Mdledle. English-Xhosa",
        "note": "Google Books also catalogues the claimed 1956 edition; the "
                "Open Library 1906 'Xosa phrase book' is an earlier edition of "
                "the same work",
    },
    # Bareau/Reding 1912 'Vocabulaire français-mobenge': the Internet Archive
    # scan of the vocabulary itself (identifier vocabulairefran00bareuoft)
    # reports possible-copyright-status NOT_IN_COPYRIGHT; US evidence only,
    # the verdict stays on the cell's 1912 year.
    (53, 1): {
        "source": "internet_archive",
        "title": "Vocabulaire français-mobenge et mobenge-français, dressé "
                 "d'après les renseignements fournis par MM. Bareau et Reding",
        "note": "an Internet Archive scan of the vocabulary itself "
                "(vocabulairefran00bareuoft) reports possible-copyright-status "
                "not-in-copyright (US evidence; the two compilers are Belgian "
                "colonial officials whose death years are unrecorded)",
    },
    # Horton's Luvale dictionary: the cell's 1975 is a real second edition
    # (bookseller and library records list a 1975 2nd ed.; the first is 1953).
    # The 1990 Google Books record that won the ranking is a later reprint.
    (35, 1): {
        "edition_year": 1975,
        "source": "abebooks",
        "title": "A Dictionary of Luvale",
        "note": "a 1975 second edition is catalogued by booksellers (the first "
                "edition is 1953); the 1990 Google Books record is a later "
                "reprint of the same work",
    },
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def load_google_key() -> str:
    key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if key:
        return key
    for path in KEY_PATHS:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0].strip()
    return ""


def as_year(text) -> int | None:
    m = YEAR_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def clean_language(text) -> str:
    """'Bubi (Kele)' -> 'Bubi'; keeps the searchable part of a language cell."""
    text = clean(text)
    text = re.sub(r"[(),;/].*$", "", text).strip()
    return text


def seq(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def tokens(text) -> set[str]:
    return {t for t in norm(text).split() if len(t) >= 3}


def coverage(ref: str, text: str) -> float:
    """share of the reference's content words that occur in the text"""
    ref_t = tokens(ref)
    if not ref_t:
        return 0.0
    return len(ref_t & tokens(text)) / len(ref_t)


def title_score(ref: str, title: str) -> float:
    if not ref or not title:
        return 0.0
    return max(seq(ref, title), coverage(ref, title) * 0.95)


def is_dict_title(title: str) -> bool:
    return bool(DICT_RE.search(title or ""))


def lc_first(text: str) -> str:
    text = clean(text)
    if "," in text:
        return norm(text.split(",")[0])
    parts = [t for t in norm(text).split() if t not in
             ("van", "von", "de", "der", "da", "di", "du", "le", "la")]
    return parts[-1] if parts else ""


def chunked(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def dedupe(items, key):
    seen, out = set(), []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

class Providers2(Providers):
    """Google Books (keyed) + HathiTrust + BnF + Wikidata on top of the base."""

    def __init__(self, http: Http, trace: Trace, gb_key: str):
        super().__init__(http, trace)
        self.gb_key = gb_key
        self.gb_dead = not bool(gb_key)
        self.gb_calls = 0
        self.gb_budget = GB_BUDGET
        self.bnf_failures = 0
        self.bnf_dead = False

    # -- cache helpers -----------------------------------------------------
    def _cached(self, key: str):
        entry = self.http._cache.get(key)
        if isinstance(entry, dict):
            return entry.get("data"), entry.get("error")
        return None, "miss"

    def _store(self, key: str, data, error=None):
        self.http._cache[key] = {"data": data, "error": error}
        self.http._dirty = True

    def _text_urllib(self, url: str, tag: str, timeout: int = 15):
        key = "TEXT::" + url
        data, err = self._cached(key)
        if err != "miss":
            return data, err
        if self.http.offline:
            return None, "offline"
        req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            self._store(key, raw)
            return raw, None
        except urllib.error.HTTPError as exc:
            err = f"HTTP {exc.code}"
        except Exception as exc:                           # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        self._store(key, None, err)
        self.trace(f"    [{tag}] {err}")
        return None, err

    def _json_urllib(self, url: str, tag: str, timeout: int = 30):
        key = "URLIB::" + url
        data, err = self._cached(key)
        if err != "miss":
            return data, err
        if self.http.offline:
            return None, "offline"
        req = urllib.request.Request(
            url, headers={"User-Agent": UA_BROWSER,
                          "Accept": "application/json,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            self._store(key, data)
            return data, None
        except urllib.error.HTTPError as exc:
            err = f"HTTP {exc.code}"
        except Exception as exc:                           # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        self._store(key, None, err)
        self.trace(f"    [{tag}] {err}")
        return None, err

    # -- Open Library edition identifiers ---------------------------------
    def ol_edition_ids(self, candidates, year):
        """OCLC/LCCN/ISBN from the edition records of the best OL work."""
        ids = []
        for c in candidates:
            if c.source != "openlibrary":
                continue
            m = re.search(r"/works/(OL\w+W)", c.url or "")
            if not m:
                continue
            data = self.http.get_json(
                f"https://openlibrary.org/works/{m.group(1)}/editions.json",
                {"limit": 50})
            for ed in (data or {}).get("entries", []):
                ey = as_year(ed.get("publish_date"))
                if year and ey and abs(ey - year) > 2:
                    continue
                for o in ed.get("oclc_numbers") or []:
                    ids.append("oclc:" + str(o))
                for l in ed.get("lccn") or []:
                    ids.append("lccn:" + str(l).split()[0])
                for i in (ed.get("isbn_13") or []) + (ed.get("isbn_10") or []):
                    digits = re.sub(r"[^0-9Xx]", "", str(i))
                    if len(digits) in (10, 13):
                        ids.append("isbn:" + digits)
            break
        return list(dict.fromkeys(ids))[:10]

    # -- Google Books ------------------------------------------------------
    def gb_query(self, query: str, limit: int = GB_RESULTS):
        """One free-text query, parsed into Candidates.

        The fielded operators (intitle:, inauthor:) no longer return results
        from this endpoint, so relevance comes from the query wording and the
        local scorer in ``score_candidates``.
        """
        if self.gb_dead:
            raise ProviderUnavailable("Google Books disabled")
        if self.gb_calls >= self.gb_budget:
            raise ProviderUnavailable("local Google Books budget reached")
        params = {"q": query, "maxResults": limit, "country": "US",
                  "printType": "books"}
        if self.gb_key:
            params["key"] = self.gb_key
        data = None
        for attempt in range(3):
            try:
                data = self.http.get_json(
                    "https://www.googleapis.com/books/v1/volumes", params)
            except ProviderUnavailable as exc:
                self.gb_dead = True
                raise ProviderUnavailable(str(exc))
            if data is not None:
                break
            time.sleep(1.0 * (attempt + 1))
        if data is None:
            return []
        self.gb_calls += 1
        out = []
        for item in data.get("items", []) or []:
            vi = item.get("volumeInfo") or {}
            ai = item.get("accessInfo") or {}
            ids = []
            for ident in vi.get("industryIdentifiers") or []:
                value = str(ident.get("identifier") or "")
                kind = ident.get("type") or ""
                if kind in ("ISBN_10", "ISBN_13"):
                    digits = re.sub(r"[^0-9Xx]", "", value)
                    if len(digits) in (10, 13):
                        ids.append("isbn:" + digits)
                m = re.match(r"(OCLC|LCCN)\s*:?\s*(\w+)", value, re.I)
                if m:
                    ids.append(m.group(1).lower() + ":" + m.group(2))
            title = clean(vi.get("title"))
            if vi.get("subtitle"):
                title = f"{title}: {vi['subtitle']}"
            out.append(Candidate(
                source="google_books",
                title=title,
                authors=list(vi.get("authors") or []),
                year=as_year(vi.get("publishedDate")),
                publisher=clean(vi.get("publisher")),
                isbn=next((i.split(":", 1)[1] for i in ids
                           if i.startswith("isbn:")), ""),
                public_domain=ai.get("publicDomain"),
                access=clean(ai.get("viewability")),
                url=("https://books.google.com/books?id=" + str(item.get("id")))
                    if item.get("id") else "",
                extra={"ids": ids, "lang": clean(vi.get("language")),
                       "volume_id": item.get("id", ""),
                       "date": clean(vi.get("publishedDate"))},
            ))
        return out

    # -- HathiTrust --------------------------------------------------------
    def hathitrust(self, identifiers: list[str]):
        """Bib API; every identifier gets its own block of records/items."""
        out = {"blocks": [], "items": [], "records": [], "error": None}
        ids = [i for i in identifiers if i]
        if not ids:
            return out
        for chunk in chunked(list(dict.fromkeys(ids))[:20], 20):
            joined = "|".join(urllib.parse.quote(x, safe="") for x in chunk)
            url = (f"https://catalog.hathitrust.org/api/volumes/brief/json/"
                   f"{joined}")
            data, err = self._json_urllib(url, "hathitrust")
            if err:
                out["error"] = err
                continue
            for key, value in (data or {}).items():
                if not isinstance(value, dict):
                    continue
                records = list((value.get("records") or {}).values())
                items = value.get("items") or []
                out["blocks"].append({"id": key, "records": records,
                                      "items": items})
                out["records"].extend(records)
                out["items"].extend(items)
        return out

    # -- BnF SRU -----------------------------------------------------------
    def bnf(self, author: str, terms: str = "") -> tuple[list[Candidate], list[dict]]:
        """French national catalogue. Returns (candidates, life_dates)."""
        if not author or self.bnf_dead:
            return [], []
        query = f'bib.author all "{author}"'
        if terms:
            query += f' and bib.title all "{terms}"'
        url = ("https://catalogue.bnf.fr/api/SRU?version=1.2"
               "&operation=searchRetrieve&query=" + urllib.parse.quote(query)
               + "&recordSchema=dublincore&maximumRecords=5")
        text, err = self._text_urllib(url, "bnf", timeout=8)
        if err or not text:
            if err and err not in ("offline",):
                self.bnf_failures += 1
                if self.bnf_failures >= 3:
                    self.bnf_dead = True
                    self.trace("[bnf] disabled for this run after three "
                               "consecutive failures")
            return [], []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return [], []

        def local(tag):
            return tag.split("}")[-1]

        out, lives = [], []
        for rec in root.iter():
            if local(rec.tag) != "record":
                continue
            fields: dict[str, list[str]] = {}
            for child in rec.iter():
                if local(child.tag) == "record" or child is rec:
                    continue
                name = local(child.tag)
                if name in ("title", "creator", "date", "publisher",
                            "language", "identifier", "subject"):
                    value = (child.text or "").strip()
                    if value:
                        fields.setdefault(name, []).append(value)
            if not fields.get("title"):
                continue
            creator = " ; ".join(fields.get("creator", []))
            life = re.search(r"\((\d{4})\s*[-–]\s*(\d{4})\)", creator)
            if life:
                lives.append({"creator": creator,
                              "birth": int(life.group(1)),
                              "death": int(life.group(2))})
            ark = next((i for i in fields.get("identifier", [])
                        if "ark:" in i), "")
            out.append(Candidate(
                source="bnf",
                title=" ; ".join(fields["title"]),
                authors=[re.sub(r"\s*\(.*", "", c).strip()
                         for c in fields.get("creator", [])],
                year=as_year(fields.get("date", [""])[0]),
                publisher=" ; ".join(fields.get("publisher", [])),
                access="catalogue_record",
                url=ark,
                extra={"lang": ",".join(fields.get("language", [])),
                       "life": {"birth": int(life.group(1)),
                                "death": int(life.group(2))} if life else {}},
            ))
        self.bnf_failures = 0
        return out, lives

    # -- Wikidata ----------------------------------------------------------
    def wikidata_death(self, name: str, year: int | None):
        if not name:
            return None
        search = self.http.get_json(
            "https://www.wikidata.org/w/api.php",
            {"action": "wbsearchentities", "search": name, "language": "en",
             "format": "json", "limit": 5, "type": "item"})
        hits = (search or {}).get("search", [])[:3]
        surname = lc_first(name)
        for hit in hits:
            label = hit.get("label", "") or ""
            if surname and surname not in norm(label):
                continue
            ent = self.http.get_json(
                "https://www.wikidata.org/w/api.php",
                {"action": "wbgetentities", "ids": hit["id"],
                 "props": "claims|labels|descriptions", "languages": "en",
                 "format": "json"})
            entity = ((ent or {}).get("entities") or {}).get(hit["id"]) or {}
            claims = entity.get("claims") or {}

            def claim_year(prop):
                for c in claims.get(prop, []):
                    value = (((c.get("mainsnak") or {}).get("datavalue") or {})
                             .get("value") or {})
                    t = value.get("time")
                    if t and re.match(r"^[+-]\d{4}", t):
                        return int(t[1:5])
                return None

            death = claim_year("P570")
            birth = claim_year("P569")
            if not death or (year and death < year - 5):
                continue
            if birth and death and not (10 <= death - birth <= 110):
                continue
            return {"source": "wikidata", "id": hit["id"], "label": label,
                    "description": hit.get("description", ""),
                    "birth": birth, "death": death,
                    "url": "https://www.wikidata.org/wiki/" + hit["id"]}
        return None

    # -- Internet Archive metadata ----------------------------------------
    def ia_meta(self, identifier: str) -> dict:
        data = self.http.get_json(
            f"https://archive.org/metadata/{urllib.parse.quote(identifier)}")
        meta = (data or {}).get("metadata") or {}
        keys = ("identifier", "title", "creator", "date", "year", "rights",
                "licenseurl", "possible-copyright-status", "copyright_date",
                "publisher", "lccn", "oclc", "isbn", "collection",
                "access-restricted-item")
        return {k: meta.get(k) for k in keys}


# --------------------------------------------------------------------------- #
# Query building and candidate scoring
# --------------------------------------------------------------------------- #

def kind_from_text(text: str) -> str:
    low = norm(text)
    for word in KIND_WORDS:
        if norm(word) in low:
            return word
    return ""


def book_concept(parsed: ParsedBook, language: str) -> str:
    """What the title should be about.

    For a URL/file-name cell the 'title' is usually the language name, so the
    concept is the language itself.  For a prose citation it is the title.
    """
    title = clean(parsed.title)
    if parsed.kind in ("url", "filename"):
        return clean_language(language) or title
    if len(tokens(title)) <= 1 and language:
        return clean_language(language)
    return title


def best_language(parsed: ParsedBook, csv_language: str) -> str:
    """Pick the language hint that belongs to *this* book.

    The Language column describes the row.  In a cell that links several
    dictionaries of different languages it is only true for one of them, so a
    file-name hint wins unless the two are clearly the same language.
    """
    csv_l = clean_language(csv_language)
    hint = clean_language(parsed.title)
    if parsed.kind not in ("url", "filename"):
        return csv_l or hint
    if not hint:
        return csv_l
    if not csv_l:
        return hint
    h, c = norm(hint), norm(csv_l)
    if c in h or h in c:
        return csv_l
    for tok in h.split():
        if len(tok) >= 4 and (tok[:4] in c or c[:4] in tok):
            return csv_l
    return hint


def given_initials(name: str) -> list[str]:
    raw = clean(name)
    if not raw:
        return []
    if "," in raw:
        given = raw.split(",", 1)[1]
    else:
        parts = raw.split()
        if len(parts) < 2:
            return []
        given = " ".join(parts[:-1])
    return [t[0] for t in norm(given).split() if t]


def given_names_compatible(query: str, label: str) -> bool:
    """Reject a namesake whose first name is different ('R. S. Roberts')."""
    q = given_initials(query)
    l = given_initials(label)
    if not q or not l:
        return True
    return q[0] == l[0]


def author_keys(parsed: ParsedBook) -> list[str]:
    keys = []
    for a in parsed.authors:
        a = clean(a)
        if not a:
            continue
        if "," in a:
            keys.append(norm(a.split(",")[0]))
        else:
            parts = [t for t in norm(a).split()
                     if t not in ("van", "von", "de", "der", "da", "di", "du",
                                  "le", "la", "and")]
            keys.append(parts[-1] if parts else "")
    return [k for k in dict.fromkeys(keys) if len(k) >= 3]


def author_match(parsed: ParsedBook, cand: Candidate) -> tuple[bool, float]:
    keys = author_keys(parsed)
    text = norm(" ".join(cand.authors))
    if not keys or not text:
        return False, 0.0
    hits = [k for k in keys if k in text]
    return bool(hits), round(len(hits) / len(keys), 2)


def score_candidate(parsed: ParsedBook, cand: Candidate, concept: str,
                    language: str) -> dict:
    ts = title_score(concept, cand.title)
    a_ok, a_score = author_match(parsed, cand)
    y_ok = bool(parsed.year and cand.year
                and abs(parsed.year - cand.year) <= 1)
    y = 1.0 if y_ok else (0.5 if (parsed.year and cand.year
                                  and abs(parsed.year - cand.year) <= 3) else 0.0)
    d = 1.0 if is_dict_title(cand.title) else 0.0
    lang = 0.0
    if language and norm(language) in norm(cand.title):
        lang = 1.0
    cand_lang = norm(cand.extra.get("lang", "") if cand.extra else "")
    if cand_lang and language and norm(language).startswith(cand_lang[:3]):
        lang = 1.0
    total = (0.30 * ts + 0.30 * a_score + 0.25 * y
             + 0.10 * d + 0.05 * lang)
    if a_ok:
        total += 0.05
    total = round(min(total, 1.0), 3)

    if total >= 0.80 and a_ok and y_ok:
        level = "exact"
    elif total >= 0.65 and a_ok:
        level = "probable"
    elif total >= 0.55 and (a_ok or y_ok):
        level = "lead"
    elif total >= 0.45:
        level = "weak"
    else:
        level = "reject"
    return {"title_sim": round(ts, 3), "author_ok": a_ok,
            "author_score": a_score, "year_ok": y_ok, "score": total,
            "level": level}


def build_queries(parsed: ParsedBook, language: str) -> list[str]:
    """A short ladder; the first two are always tried."""
    language = clean_language(language)
    author = " ".join(author_keys(parsed)[:1]) or parsed.query_author
    concept = book_concept(parsed, language)
    kind = kind_from_text(f"{parsed.title} {' '.join(parsed.notes)} "
                          f"{parsed.full_author}")
    queries = []

    if parsed.kind == "citation" and len(tokens(concept)) >= 2:
        # a real title is the strongest query we have
        queries.append(f"{short_title(concept)} {author}".strip())
        if language:
            queries.append(f"{author} {language} {kind or 'dictionary'}".strip())
        queries.append(short_title(concept, 6))
    elif language:
        primary = kind or "dictionnaire"
        secondary = "dictionary" if primary != "dictionary" else "dictionnaire"
        queries.append(f"{author} {language} {primary}".strip())
        queries.append(f"{author} {language} {secondary}".strip())
        queries.append(f"{language} {primary}".strip())
    elif author:
        queries.append(f"{author} {kind or 'dictionary'}".strip())
        queries.append(f"{author} {concept}".strip())
    else:
        queries.append(concept)

    out, seen = [], set()
    for q in queries:
        q = re.sub(r"[\"'()]+", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:4]


def run_lookups(providers: Providers2, parsed: ParsedBook, language: str,
                trace, cell: str):
    """Query the catalogues; return (candidates, bnf_lives, source_errors)."""
    queries = build_queries(parsed, language)
    trace("QUERIES")
    for q in queries:
        trace(f"  - {q!r}")

    candidates: list[Candidate] = []
    lives: list[dict] = []
    errors: dict[str, str] = {}

    def add(items):
        for c in items or []:
            candidates.append(c)

    with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        jobs = {}
        # Open Library / Internet Archive / Crossref on the richest query
        main_q = queries[0] if queries else clean(str(cell))[:100]
        jobs[pool.submit(providers.openlibrary, main_q, parsed=parsed)] = "openlibrary"
        jobs[pool.submit(providers.internet_archive,
                         re.sub(r'[:()"\[\]{}!^~?\\/]', " ", main_q)[:120])] = "internet_archive"
        # Crossref has essentially no pre-1970 book records; it only adds noise
        # (and false "corroboration") for the colonial dictionaries.
        if parsed.year is None or parsed.year >= 1970:
            jobs[pool.submit(providers.crossref, main_q)] = "crossref"

        author = author_keys(parsed)[0] if author_keys(parsed) else ""
        bnf_terms = clean(language)
        if author and (parsed.year is None or parsed.year <= 1980):
            jobs[pool.submit(providers.bnf, author, bnf_terms)] = "bnf"

        for q in queries[:3]:
            jobs[pool.submit(providers.gb_query, q)] = "google_books"

        for fut in futures.as_completed(jobs):
            name = jobs[fut]
            try:
                result = fut.result()
            except ProviderUnavailable as exc:
                errors[name] = str(exc)
                trace(f"[{name}] unavailable: {exc}")
                continue
            except Exception as exc:                       # noqa: BLE001
                errors[name] = f"{type(exc).__name__}: {exc}"
                trace(f"[{name}] error: {exc}")
                continue
            if name == "bnf":
                recs, bnf_lives = result
                add(recs)
                lives.extend(bnf_lives)
            else:
                add(result)

    trace(f"CANDIDATES {len(candidates)} "
          f"({', '.join(f'{k}={len([c for c in candidates if c.source == k])}' for k in sorted({c.source for c in candidates})) or 'none'})")
    return candidates, lives, errors


def pick_edition(candidates: list[Candidate], parsed: ParsedBook,
                 concept: str, language: str, trace):
    """Score, filter and return (best, support, scored) for one book."""
    scored = []
    for c in candidates:
        s = score_candidate(parsed, c, concept, language)
        # guard against a same-title work from a wildly different period
        if (parsed.year and c.year and abs(parsed.year - c.year) > 25
                and s["title_sim"] < 0.9):
            s["level"] = "reject"
        scored.append((c, s))

    accepted = [(c, s) for c, s in scored
                if s["level"] in ("exact", "probable", "lead", "weak")]
    # Lookups finish in thread order, so the candidate list itself varies
    # between runs.  Break every score tie on stable record fields, otherwise
    # the picked edition (and the review flag) can flip from run to run.
    accepted.sort(key=lambda cs: (
        -cs[1]["score"], "-google" not in cs[0].source, cs[0].source,
        (cs[0].title or "").lower(), cs[0].year or 9999, cs[0].url or ""))
    for c, s in [(c, s) for c, s in scored if s["score"] >= 0.45][:5]:
        trace(f"    {s['level']:8s} {s['score']:.2f} "
              f"(t={s['title_sim']:.2f} a={int(s['author_ok'])} "
              f"y={int(s['year_ok'])}) {c.year or '????'} "
              f"{c.source:14s} {c.title[:58]!r}")
    if not accepted:
        return None, [], scored

    best = accepted[0][0]
    best_s = accepted[0][1]

    def agrees(a: Candidate, b: Candidate) -> bool:
        if a.year and b.year and abs(a.year - b.year) > 2:
            return False
        return max(seq(a.title, b.title),
                   coverage(a.title, b.title),
                   coverage(b.title, a.title)) >= 0.6

    support = [best]
    for c, _ in accepted[1:]:
        if agrees(c, best):
            support.append(c)
    return (best, support, scored) if best_s["level"] != "weak" else (None, [], scored)


_HONORIFIC_RE = re.compile(
    r"^(rev|bp|bishop|sir|dr|prof|mr|mrs|ms|m|fr|p[èe]re|abbe|abb[ée])\.?\s+",
    re.IGNORECASE)


def clean_author_name(a: str) -> str:
    """Tidy a catalogue author string:parens, honorifics, role suffixes."""
    if not a:
        return ""
    a = re.sub(r"\(.*?\)", " ", a)
    a = a.replace("..", ".")
    a = re.sub(r"\s+(Auteur du texte|Auteur|Hrsg|edited by|ed\.)\s*$", " ",
               a, flags=re.I)
    for _ in range(3):
        a = _HONORIFIC_RE.sub("", a.strip())
    a = person_name_from(a)
    return re.sub(r"\s+", " ", a).strip(" ,. \"")


def classify_edition(parsed_year: int | None, cat_year: int | None,
                     best: Candidate | None) -> tuple[str, str]:
    """Compare the cell's year with the matched catalogue edition.

    Returns (edition_match, note).  A much later catalogue record is a modern
    reprint/facsimile when its publisher is a POD/reprint house or the gap is
    large; such a reissue confirms only that the work exists and does not say
    anything about the edition the cell points at, so no review flag and the
    verdict stays on the cell year.
    """
    if not parsed_year or not cat_year:
        return "n/a", ""
    gap = cat_year - parsed_year
    if abs(gap) <= 2:
        return "same", ""
    publisher = clean(getattr(best, "publisher", "") if best else "")
    if publisher and POD_PUBLISHER_RE.search(publisher):
        return "reprint", (
            f"matched record dated {cat_year} is from the reprint house "
            f"{publisher!r}: a modern reissue of the {parsed_year}-era work")
    if abs(gap) >= 40:
        return "reprint", (
            f"matched record dated {cat_year} ({gap} years later than the cell): "
            "treated as a modern reprint of the work; verdict uses the cell year")
    if gap < 0:
        return "earlier_edition", (
            f"matched record is an earlier edition ({cat_year}, the cell says "
            f"{parsed_year})")
    return "other", ""


# --------------------------------------------------------------------------- #
# Per-book resolution
# --------------------------------------------------------------------------- #

PER_BOOK_FIELDS = [
    "book_no", "book_source", "book_reference", "parsed_author",
    "parsed_title", "parsed_year", "parsed_year_original",
    "year_used", "year_source", "identity_level", "identity_basis",
    "matched_title", "matched_year", "matched_sources", "source_agreement",
    "edition_match", "author_death_year", "death_source",
    "us_status", "eu_status", "rights_overall", "rights_confidence",
    "verification", "needs_review", "review_reason", "public_domain_note",
    "evidence_google", "evidence_hathitrust", "evidence_ia", "evidence_bnf",
    "evidence", "rights_notes",
]

ROW_FIELDS = [
    "book_parse_type", "parsed_author", "parsed_title", "parsed_year",
    "parsed_year_original", "parsed_publisher",
    "year_used", "year_source", "identity_level", "identity_basis",
    "matched_title", "matched_year", "matched_sources", "source_agreement",
    "edition_match", "author_death_year", "death_source",
    "us_status", "eu_status", "rights_overall", "rights_confidence",
    "verification", "us_basis", "eu_basis",
    "evidence_google", "evidence_hathitrust", "evidence_ia", "evidence_bnf",
    "needs_review", "review_reason", "evidence", "rights_notes",
    "books_in_cell", "books_summary", "public_domain_books",
]


def book_reference(n: int, parsed: ParsedBook) -> str:
    who = parsed.query_author or parsed.full_author or "?"
    year = parsed.year if parsed.year else "n.d."
    source = ""
    if parsed.urls:
        source = Path(urllib.parse.urlparse(parsed.urls[0]).path).name
    return f"{n}) {who} {year}" + (f" [{source}]" if source else "")


def book_source(parsed: ParsedBook) -> str:
    if parsed.urls:
        return Path(urllib.parse.urlparse(parsed.urls[0]).path).name
    return ""


def _pd_note(us: str, eu: str) -> str:
    pd = ("public_domain", "presumed_public_domain")
    us_pd, eu_pd = us in pd, eu in pd
    if us_pd and eu_pd:
        return "PD in both USA+EU"
    if us_pd:
        return f"PD in USA only ({us.replace('_', ' ')})"
    if eu_pd:
        return f"PD in EU only ({eu.replace('_', ' ')})"
    return ""


def resolve_book(idx: int, parsed: ParsedBook, providers: Providers2,
                 trace, language: str, iso: str, cell: str,
                 book_no: int = 1) -> dict:
    if len(parsed.authors) > 1:
        # 'ruskin-ruskin_lingombe1937.pdf' parses to two identical surnames
        parsed.authors = list(dict.fromkeys(
            a for a in parsed.authors if clean(a)))
    language = best_language(parsed, language)
    row = {f: "" for f in ROW_FIELDS}
    row.update(book_parse_type=parsed.kind, parsed_author=parsed.full_author,
               parsed_title=parsed.title, parsed_year=parsed.year or "",
               parsed_year_original=parsed.original_year or "",
               parsed_publisher=parsed.publisher)

    trace(f"PARSE  kind={parsed.kind} author={parsed.full_author!r} "
          f"title={parsed.title!r} year={parsed.year} "
          f"language={language!r}")
    if parsed.kind in ("empty", "bibkey", "junk"):
        row.update(us_status="unknown", eu_status="unknown",
                   rights_overall="unknown", rights_confidence="none",
                   verification="unresolved", needs_review="yes",
                   review_reason="no bibliographic data in the cell",
                   rights_notes="; ".join(parsed.notes))
        return row

    concept = book_concept(parsed, language)
    extra_candidates: list[Candidate] = []
    if parsed.kind == "archive_item":
        # the item page itself is a catalogue record: read its metadata
        m = re.search(r"details/([A-Za-z0-9._-]+)", cell)
        ident = m.group(1) if m else ""
        if ident:
            meta = providers.ia_meta(ident)
            title = clean(meta.get("title"))
            if title:
                creators = meta.get("creator")
                if isinstance(creators, str):
                    creators = [creators]
                creators = [clean(c) for c in (creators or []) if clean(c)]
                year_meta = as_year(meta.get("date") or meta.get("year"))
                parsed.title = title
                parsed.authors = creators[:2] or parsed.authors
                parsed.year = year_meta or parsed.year
                extra_candidates.append(Candidate(
                    source="internet_archive", title=title,
                    authors=creators, year=year_meta,
                    publisher=clean(meta.get("publisher")),
                    public_domain=bool(re.search(
                        r"public domain|not in copyright",
                        clean(meta.get("possible-copyright-status"))
                        + " " + clean(meta.get("rights")), re.I)) or None,
                    access="item", url="https://archive.org/details/" + ident,
                    extra={"identifier": ident,
                           "ids": [f"lccn:{clean(meta.get('lccn')).split()[0]}"]
                           if clean(meta.get("lccn")) else []}))
                trace(f"       archive.org metadata -> {title!r} "
                      f"{creators[:2]} {year_meta}")

    candidates, bnf_lives, errors = run_lookups(
        providers, parsed, language, trace, cell)
    candidates = extra_candidates + candidates

    # ---- edition ---------------------------------------------------------
    best, support, scored = pick_edition(candidates, parsed, concept,
                                         language, trace)
    sources = sorted({c.source for c in support}) if support else []
    agreement = len(sources)

    if best is None:
        identity, basis = "none", "no catalogue hit with title+author agreement"
    elif agreement >= 2 and scored:
        identity = "corroborated"
        basis = "title+year agree across " + ", ".join(sources)
    elif agreement == 1:
        identity = "single_source"
        basis = f"one catalogue record ({sources[0]})"
    else:
        identity = "none"
        basis = "no catalogue hit"

    cat_year = best.year if best else None
    year = parsed.year or cat_year
    if parsed.year:
        year_source = "book_cell"
    elif cat_year:
        year_source = "catalogue"
    else:
        year_source = "none"

    edition_match, edition_note = classify_edition(parsed.year, cat_year, best)
    # a hand-verified catalogue record for one of the edition mismatches (or
    # the only record with a year, when the cell itself is undated)
    manual = EDITION_VERIFICATIONS.get((idx, book_no))
    if manual:
        note = (f"verified record [{manual.get('source')}]:"
                f" {manual.get('title', '')}: {manual.get('note', '')}")
        parsed.notes.append(note)
        trace("MANUAL  " + note)
        if manual.get("edition_year"):
            if not parsed.year:
                year = manual["edition_year"]
                year_source = f"verified:{manual['source']}"
                if identity == "none":
                    identity = "verified_source"
                    basis = (f"{manual['source']} record of the same work "
                             f"({year})")
                edition_match = "same"
                edition_note = ""
            elif abs(parsed.year - manual["edition_year"]) <= 2:
                edition_match = "same"
                edition_note = ""

    row.update(identity_level=identity, identity_basis=basis,
               matched_title=best.title if best else "",
               matched_year=cat_year or "",
               matched_sources=",".join(sources),
               source_agreement=str(agreement),
               edition_match=edition_match,
               year_used=year or "", year_source=year_source)
    trace(f"IDENTITY {identity} | agreement={agreement} ({','.join(sources) or '-'}) "
          f"| edition={edition_match} | year={year} ({year_source})")

    # ---- identifiers -> HathiTrust --------------------------------------
    ids = []
    for c in support or ([best] if best else []):
        extra = c.extra or {}
        ids.extend(extra.get("ids") or [])
        if c.isbn:
            ids.append("isbn:" + re.sub(r"[^0-9Xx]", "", c.isbn))
    ia_ids = []
    for c in (support or [])[:4]:
        if c.source == "internet_archive":
            ident = (c.extra or {}).get("identifier")
            if not ident:
                m = re.search(r"details/([A-Za-z0-9._-]+)", c.url or "")
                ident = m.group(1) if m else ""
            if ident:
                ia_ids.append(ident)

    # Internet Archive metadata often carries the LCCN/OCLC that unlocks
    # HathiTrust even when the catalogues above did not.
    ia_pd = False
    ia_status = ""
    for ident in ia_ids[:2]:
        meta = providers.ia_meta(ident)
        status = clean(meta.get("possible-copyright-status"))
        if status:
            ia_status = status
        if re.search(r"public domain|not in copyright|pd",
                     status + " " + clean(meta.get("rights")), re.I):
            ia_pd = True
        for key in ("lccn", "oclc", "isbn"):
            value = meta.get(key)
            values = value if isinstance(value, list) else [value]
            for v in values:
                v = clean(v).split()[0] if clean(v) else ""
                if not v:
                    continue
                if key == "lccn":
                    ids.append("lccn:" + v)
                elif key == "oclc":
                    ids.append("oclc:" + v)
                else:
                    digits = re.sub(r"[^0-9Xx]", "", v)
                    if len(digits) in (10, 13):
                        ids.append("isbn:" + digits)

    # Open Library edition records carry OCLC/LCCN that the work record hides.
    if any(c.source == "openlibrary" for c in (support or [])):
        ids.extend(providers.ol_edition_ids(support, year))

    ids = list(dict.fromkeys(i for i in ids if i))[:20]

    gb_support = [c for c in (support or []) if c.source == "google_books"]
    gb_pd_cand = next((c for c in gb_support
                       if c.public_domain and c.access == "ALL_PAGES"), None)
    row["evidence_google"] = "; ".join(
        f"{c.year or '?'} publicDomain={c.public_domain} view={c.access}"
        for c in gb_support[:3]) or "-"

    ht = providers.hathitrust(ids) if ids else {"blocks": [], "items": []}
    ht_codes, ht_dates, ht_edition = [], [], "n/a"
    for block in ht.get("blocks", []):
        dates = [as_year(d) for r in block["records"]
                 for d in (r.get("publishDates") or [])]
        dates = [d for d in dates if d]
        ok = (not year or not dates
              or any(abs(d - year) <= 2 for d in dates))
        if ok:
            ht_dates.extend(dates)
            ht_codes.extend((i.get("rightsCode") or "?")
                            for i in block["items"])
    if ht.get("blocks"):
        if not year:
            ht_edition = "unknown"
        elif not ht_dates:
            ht_edition = "unknown"
        elif any(abs(d - year) <= 2 for d in ht_dates):
            ht_edition = "ok"
        else:
            ht_edition = "mismatch"
    row["evidence_hathitrust"] = (
        f"ids={len(ids)}; codes={','.join(sorted(set(ht_codes))) or '-'}; "
        f"dates={';'.join(str(d) for d in sorted(set(ht_dates))) or '-'}; "
        f"edition={ht_edition}")
    if ht_codes:
        trace(f"    hathitrust codes={sorted(set(ht_codes))} "
              f"dates={sorted(set(ht_dates))} edition={ht_edition}")

    # ---- Internet Archive rights ----------------------------------------
    row["evidence_ia"] = f"status={ia_status}" if ia_status else ""
    bnf_records = [c for c in candidates if c.source == "bnf"]
    row["evidence_bnf"] = (
        f"records={len(bnf_records)}"
        + (f"; life={bnf_lives[0]['birth']}-{bnf_lives[0]['death']}"
           if bnf_lives else "")) if bnf_records else ""

    # ---- author death ----------------------------------------------------
    def _death_for(qname: str) -> tuple[int | None, str]:
        """person_death then wikidata_death, both namesake-guarded."""
        qname = re.sub(r"\(.*", "", qname)
        qname = re.sub(r"\s+", " ", qname).strip(" ,.")
        if len(qname.split()) < 2:
            return None, ""
        info = None
        try:
            info = providers.person_death(qname, year)
        except Exception as exc:                           # noqa: BLE001
            trace(f"    [death] lookup failed for {qname!r}: {exc}")
        if info and info.get("death"):
            if given_names_compatible(qname, info.get("label", "")):
                trace(f"    death {info['death']} from {info['source']} "
                      f"({info.get('label')})")
                return info["death"], info["source"]
            trace(f"    [death] rejected namesake {info.get('label')!r} "
                  f"(first name differs from {qname!r})")
        wd = providers.wikidata_death(qname, year)
        if wd and wd.get("death") \
                and given_names_compatible(qname, wd.get("label", "")):
            trace(f"    death {wd['death']} from wikidata ({wd.get('label')})")
            return wd["death"], "wikidata"
        return None, ""

    name = parsed.person_name
    if len(name.split()) < 2 and best and best.authors:
        name = clean_author_name(best.authors[0])

    # for multi-author books every author (and any fuller name the matched
    # catalogue records give) is looked up, because the EU term runs from the
    # last surviving author
    seen: set[str] = set()
    wanted: list[str] = []
    if len(name.split()) >= 2:
        wanted.append(name)
    if len(parsed.authors) > 1:
        pool = list(parsed.authors)
        for c in (support or [best] if best else []):
            if c.authors:
                pool.extend(c.authors)
        for a in pool:
            nm = clean_author_name(a)
            if nm and len(nm.split()) >= 2 and norm(nm) not in seen:
                seen.add(norm(nm))
                wanted.append(nm)
    wanted = wanted[:5]
    trace(f"DEATHWANT {wanted!r}")

    resolved = {w: _death_for(w) for w in wanted}
    death = None
    death_source = ""
    if name in resolved:
        death, death_source = resolved[name]
    else:
        for w, (d, s) in resolved.items():
            if d:
                death, death_source = d, s
                break
    if not death and bnf_lives:
        keys = set(author_keys(parsed))
        for life in bnf_lives:
            if any(k and k in norm(life["creator"]) for k in keys):
                death = life["death"]
                death_source = "bnf-life-dates"
                trace(f"    death {death} from BnF creator field")
                break

    notes = list(parsed.notes)
    if edition_note:
        notes.append(edition_note)
    death = _plausible_death(death, year, notes, trace)
    if death is None:
        death_source = ""

    # for multi-author works: a death late enough that the EU term has not
    # expired is in copyright regardless of the other authors' (unknown)
    # deaths; a death early enough proves PD only when *all* deaths are known
    multi_deaths: dict[str, int] = {}
    n_authors = max(1, len(parsed.authors))
    if n_authors > 1 and wanted:
        for w in wanted:
            d, s = resolved.get(w, (None, ""))
            if d and _plausible_death(d, year, notes, trace):
                multi_deaths[w] = d
    if multi_deaths:
        # display the death of the last surviving author
        known_d = max(multi_deaths.values())
        if death is None or known_d >= death:
            death = known_d
            death_source = next(
                s for w, (d, s) in resolved.items() if d == known_d)
    row["author_death_year"] = death or ""
    row["death_source"] = death_source

    # ---- statutory terms -------------------------------------------------
    us, us_basis = us_rights(year, AS_OF_YEAR)
    all_found = n_authors == 1 or len(multi_deaths) >= n_authors
    eu, eu_basis = eu_rights(year, death if all_found else None,
                             n_authors, AS_OF_YEAR)
    if n_authors > 1 and multi_deaths:
        known_d = max(multi_deaths.values())
        if all_found:
            probe_eu, probe_basis = eu_rights(year, known_d, 1, AS_OF_YEAR)
            eu, eu_basis = probe_eu, (
                f"term runs from the last surviving author (died {known_d}; "
                f"all author deaths found); {probe_basis}")
        else:
            probe_eu, _ = eu_rights(year, known_d, 1, AS_OF_YEAR)
            if probe_eu == "in_copyright":
                eu, eu_basis = "in_copyright", (
                    f"author died {known_d}: the last surviving author cannot "
                    f"have died earlier, so the EU term runs past "
                    f"{AS_OF_YEAR}")
    ev_us, ev_eu = [], []

    codes = sorted(set(ht_codes))
    gb_pd = bool(gb_pd_cand is not None and year
                 and (gb_pd_cand.year is None or abs(gb_pd_cand.year - year) <= 2))
    if gb_pd:
        ev_us.append(f"Google Books serves the full view and marks publicDomain "
                     f"(volume {gb_pd_cand.extra.get('volume_id', '')}, "
                     f"{gb_pd_cand.year})")
    if "pd" in codes:
        ev_us.append("HathiTrust rightsCode pd")
        ev_eu.append("HathiTrust rightsCode pd (worldwide)")
    elif "pdus" in codes:
        ev_us.append("HathiTrust rightsCode pdus")
    if "ic" in codes:
        ev_us.append("HathiTrust rightsCode ic")
    if ia_pd:
        ev_us.append(f"Internet Archive reports {ia_status or 'public domain'}")

    conflict = ""
    if gb_pd and "ic" in codes:
        conflict = ("Google Books serves the full view (publicDomain=true) but "
                    "HathiTrust marks the matching item ic")
    if "pd" in codes:
        us, eu = "public_domain", "public_domain"
        us_basis = "HathiTrust marks the matching edition pd (public domain worldwide)"
        eu_basis = us_basis
    elif "pdus" in codes and us != "public_domain":
        us = "public_domain"
        us_basis = "HathiTrust marks the matching edition pdus (public domain in the USA)"
    elif gb_pd and us != "public_domain":
        us = "public_domain"
        us_basis = ("Google Books publicDomain=true for the matching edition "
                    "(full view); the statutory term is the same or later")
    elif "ic" in codes and us == "presumed_in_copyright":
        us = "in_copyright"
        us_basis = "HathiTrust marks the matching edition ic; " + us_basis
    if codes and "pd" not in codes and "pdus" not in codes and "ic" not in codes:
        ev_us.append("HathiTrust rightsCode " + ",".join(codes))

    overall_status = overall(us, eu)

    # ---- verification level ---------------------------------------------
    if conflict:
        verification = "conflict"
    elif "pd" in codes or ("pdus" in codes and year) or gb_pd:
        verification = "verified"
    elif identity == "corroborated":
        verification = "verified"
    elif identity == "single_source":
        verification = "single_source"
    elif year:
        verification = "rule_only"
    else:
        verification = "unresolved"

    if verification == "verified" and not conflict:
        confidence = "high"
    elif year_source == "book_cell":
        confidence = "medium"
    elif year:
        confidence = "low"
    else:
        confidence = "none"

    # ---- review flag -----------------------------------------------------
    reasons = []
    if not year:
        reasons.append("no publication year in the cell or in any catalogue")
    if identity == "none" and year_source != "book_cell":
        reasons.append("identity not established and the year is not from the cell")
    if conflict:
        reasons.append(conflict)
    if edition_match == "other" or edition_match == "earlier_edition":
        cell_us = us_rights(parsed.year, AS_OF_YEAR)[0]
        cat_us = us_rights(cat_year, AS_OF_YEAR)[0]
        if cell_us != cat_us:
            reasons.append(
                f"catalogue edition is {cat_year} and the cell says {parsed.year}; "
                f"the US status differs between the two")
    if len(parsed.authors) > 1 and eu.startswith("unknown"):
        reasons.append("multi-author EU term: only the first author's death is known")
    needs_review = "yes" if reasons else "no"

    evidence = "; ".join(ev_us + [e for e in ev_eu if e not in ev_us])
    row["evidence"] = evidence
    row.update(us_status=us, eu_status=eu, rights_overall=overall_status,
               rights_confidence=confidence, verification=verification,
               us_basis=us_basis, eu_basis=eu_basis,
               needs_review=needs_review,
               review_reason="; ".join(reasons),
               rights_notes="; ".join(dict.fromkeys(notes)))

    trace(f"DECISION US={us} ({us_basis})")
    trace(f"         EU={eu} ({eu_basis})")
    trace(f"         overall={overall_status} confidence={confidence} "
          f"verification={verification} review={needs_review}")
    return row


def resolve_row(idx: int, value, providers: Providers2, language: str,
                iso: str, trace) -> tuple[dict, list[dict]]:
    books = parse_cell_all(value)
    n_books = len(books)
    recs, rows = [], []
    for n, parsed in enumerate(books, 1):
        trace.rule("-")
        trace(f"ROW {idx:03d} BOOK {n}/{n_books} | cell: {str(value)[:110]!r}")
        row = resolve_book(idx, parsed, providers, trace, language, iso,
                           str(value), book_no=n)
        rows.append(row)
        rec = {"row_index": idx, "book_no": n, "books_in_cell": n_books}
        for f in PER_BOOK_FIELDS[1:]:
            rec[f] = row.get(f, "")
        rec["book_source"] = book_source(parsed)
        rec["book_reference"] = book_reference(n, parsed)
        rec["public_domain_note"] = _pd_note(row["us_status"], row["eu_status"])
        recs.append(rec)

    if n_books == 1:
        row = dict(rows[0])
        row["books_in_cell"] = 1
        row["books_summary"] = (
            f"{recs[0]['book_reference']} -> US={row['us_status']}, "
            f"EU={row['eu_status']}")
        row["public_domain_books"] = (
            f"{recs[0]['book_reference']}: {recs[0]['public_domain_note']}"
            if recs[0]["public_domain_note"] else "none")
        return row, recs

    rank = ["public_domain", "presumed_public_domain", "in_copyright",
            "presumed_in_copyright", "unknown"]
    us = min((r["us_status"] for r in rows),
             key=lambda s: rank.index(s) if s in rank else len(rank))
    eu = min((r["eu_status"] for r in rows),
             key=lambda s: rank.index(s) if s in rank else len(rank))
    agg = overall(us, eu)
    if len({r["rights_overall"] for r in rows}) > 1:
        agg += " [some books]"
    row = dict(rows[0])
    row.update(us_status=us, eu_status=eu, rights_overall=agg,
               books_in_cell=n_books,
               books_summary="; ".join(
                   f"{r['book_reference']} -> US={r['us_status']}, "
                   f"EU={r['eu_status']}" for r in recs),
               public_domain_books="; ".join(
                   f"{r['book_reference']}: {r['public_domain_note']}"
                   for r in recs if r["public_domain_note"]) or "none")
    row["needs_review"] = ("yes" if any(r["needs_review"] == "yes"
                                       for r in rows) else "no")
    row["review_reason"] = " | ".join(
        f"{rec['book_reference']}: {rec['review_reason']}"
        for rec, r in zip(recs, rows) if r["review_reason"])
    return row, recs


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

def _clip(text, n=52) -> str:
    text = str(text or "").replace("|", "/")
    return text if len(text) <= n else text[:n - 1] + "…"


def write_summary(out, per_book, log_path: Path) -> None:
    from collections import Counter

    if per_book is None or per_book.empty:
        return
    n = len(out)
    books = len(per_book)
    ident = Counter(per_book["identity_level"])
    ver = Counter(per_book["verification"])
    conf = Counter(per_book["rights_confidence"])
    review = per_book[per_book["needs_review"] == "yes"]
    pd = per_book[per_book["public_domain_note"] != ""]
    src = Counter()
    for value in per_book["matched_sources"].fillna(""):
        for s in str(value).split(","):
            if s:
                src[s] += 1

    lines = [
        "# Rights resolution — full run",
        "",
        f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')} · cells: {n} · "
        f"books: {books} · as-of year: {AS_OF_YEAR}",
        "",
        "Method: the pilot's evidence chain, run over the full table. Google "
        "Books (keyed) supplies edition dates and a `publicDomain`/full-view "
        "flag; Open Library supplies edition-level `publish_date`; Internet "
        "Archive and BnF corroborate; HathiTrust `rightsCode` is accepted only "
        "when its record dates match the edition. VIAF → Wikipedia → Wikidata "
        "→ BnF life dates supply author deaths. Verdicts are the statutory US "
        "(95 years) and EU (life+70) terms, reconciled with that evidence.",
        "",
        "## Coverage",
        "",
        "| Measure | Books |",
        "| --- | --- |",
        f"| Books in the table | {books} |",
        f"| Edition corroborated by 2+ catalogues | {ident.get('corroborated', 0)} |",
        f"| Edition from one catalogue | {ident.get('single_source', 0)} |",
        f"| No edition match | {ident.get('none', 0)} |",
        f"| Catalogue record is a modern reprint of the cited work "
        f"| {(per_book['edition_match'] == 'reprint').sum()} |",
        f"| Verified (2+ catalogues, or a HathiTrust/Google Books flag) | {ver.get('verified', 0)} |",
        f"| Single catalogue only | {ver.get('single_source', 0)} |",
        f"| Statutory rule only (year from the cell) | {ver.get('rule_only', 0)} |",
        f"| Unresolved | {ver.get('unresolved', 0)} |",
        f"| **Needs a human** | **{len(review)}** |",
        "",
        "Catalogue hits used for identity: " + (
            ", ".join(f"{k} {v}" for k, v in src.most_common()) or "none") + ".",
        "",
        "## Verdicts",
        "",
        "| Status | Books |",
        "| --- | --- |",
    ]
    for status, count in per_book["rights_overall"].value_counts().items():
        lines.append(f"| `{status}` | {count} |")
    lines += [
        "",
        "| Confidence | Books |",
        "| --- | --- |",
    ]
    for level in ("high", "medium", "low", "none"):
        lines.append(f"| {level} | {conf.get(level, 0)} |")

    lines += [
        "",
        "## Books that still need a human",
        "",
        f"{len(review)} of {books} books could not be decided from a lookup "
        "service or the statutory rule alone.",
        "",
    ]
    if len(review):
        lines += [
            "| row | book | identity | year | why |",
            "| --- | --- | --- | --- | --- |",
        ]
        for _, r in review.iterrows():
            lines.append(
                f"| {r['row_index']} | {_clip(r['book_reference'], 48)} | "
                f"{r['identity_level']} | {r['year_used'] or '—'} | "
                f"{_clip(r['review_reason'], 110)} |")
    else:
        lines.append("None.")

    lines += [
        "",
        "## Public domain",
        "",
        f"{len(pd)} of {books} books are public domain somewhere; the roll-up "
        "per cell is in `sigtyp_sub_dicts_with_rights_v2.csv`.",
        "",
        "| row | book | US | EU | evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for _, r in pd.iterrows():
        lines.append(
            f"| {r['row_index']} | {_clip(r['book_reference'], 40)} | "
            f"{r['us_status']} | {r['eu_status']} | "
            f"{_clip(r['evidence'], 80)} |")

    lines += [
        "",
        "## Hand-checked records",
        "",
        "Books where one extra lookup replaced a false edition mismatch "
        "(the entry table is `EDITION_VERIFICATIONS` in `resolve_rights.py`).",
        "",
        "| row | book | what the lookup established |",
        "| --- | --- | --- |",
    ]
    for (ri, bn), m in sorted(EDITION_VERIFICATIONS.items()):
        lines.append(f"| {ri} | {bn} | {_clip(m.get('note', ''), 170)} |")

    lines += [
        "",
        "## Where the answers came from",
        "",
        f"- Google Books answered for {src.get('google_books', 0)} books "
        "(edition date + `publicDomain`; fielded `intitle:`/`inauthor:` "
        "operators no longer work, so candidates are ranked locally).",
        f"- HathiTrust item records were reached for "
        f"{sum(1 for v in per_book['evidence_hathitrust'].fillna('') if 'codes=' in v and 'codes=-' not in v)} books "
        "(identifier coverage is the limit, as in the pilot).",
        f"- BnF matched {src.get('bnf', 0)} books and supplies life dates in "
        "the creator field.",
        "- Internet Archive was not fetched from the gramfinder host; URL "
        "cells are resolved from the file-name hints plus the catalogues.",
        "",
        f"Full trace: `{log_path.name}`. Per-book table: `{PER_BOOK_CSV.name}`.",
    ]
    SUMMARY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def backup_outputs() -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / stamp
    dest.mkdir(exist_ok=True)
    for path in (OUTPUT_CSV, PER_BOOK_CSV, SUMMARY_FILE, LOG_FILE,
                 ROOT / "sigtyp_sub_dicts_with_rights.csv",
                 ROOT / "sigtyp_sub_dicts_rights_per_book.csv",
                 ROOT / "rights_summary.md"):
        if path.exists():
            (dest / path.name).write_bytes(path.read_bytes())
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--input", default=str(INPUT_CSV))
    ap.add_argument("--output", default=str(OUTPUT_CSV))
    ap.add_argument("--per-book-output", default=str(PER_BOOK_CSV))
    ap.add_argument("--log", default=str(LOG_FILE))
    ap.add_argument("--cache", default=str(CACHE_FILE))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rows", default="")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args(argv)

    # the log/cache live in SIGTYP/logs, which may not exist in a fresh clone
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    Path(args.cache).parent.mkdir(parents=True, exist_ok=True)

    key = load_google_key()
    trace = Trace(Path(args.log), echo=not args.quiet)
    http = Http(Path(args.cache), args.offline, trace)

    # catalogue and authority APIs occasionally stall for 20-60 s; a short
    # timeout keeps the run bounded and the cache makes retries cheap
    _raw_get = http.get_json

    def _get(url, params=None, timeout=REQUEST_TIMEOUT):
        return _raw_get(url, params, timeout=timeout)

    http.get_json = _get  # type: ignore[method-assign]
    providers = Providers2(http, trace, key)

    df = pd.read_csv(args.input)
    work = df.head(args.limit) if args.limit else df
    if args.rows:
        wanted = [int(x) for x in re.split(r"[,\s]+", args.rows.strip()) if x]
        work = df.loc[[i for i in wanted if i in df.index]]

    if not args.no_backup:
        dest = backup_outputs()
        trace(f"[backup] existing outputs copied to {dest}")

    trace.rule()
    trace("SIGTYP RIGHTS RESOLUTION (pilot chain, full run)")
    trace(f"started {datetime.now().isoformat(timespec='seconds')} | "
          f"{len(work)} rows | Google Books key: "
          f"{'present' if key else 'MISSING'}")
    trace(f"as-of {AS_OF_YEAR}: US PD <= {AS_OF_YEAR - 96}; "
          f"EU PD if death <= {AS_OF_YEAR - 71}")
    trace.rule()

    rows_by_index: dict[int, dict] = {}
    per_book: list[dict] = []

    def run_one(idx: int, value, language: str, iso: str):
        local_lines = []

        class Local:
            def __call__(self, msg="", indent=0):
                local_lines.append(("    " * indent) + msg)

            def rule(self, char="-"):
                local_lines.append(char * 80)

        t0 = time.time()
        try:
            row, recs = resolve_row(idx, value, providers, language, iso, Local())
        except Exception as exc:                           # noqa: BLE001
            local_lines.append(f"ROW {idx:03d} FAILED: {type(exc).__name__}: {exc}")
            row = {f: "" for f in ROW_FIELDS}
            row.update(book_parse_type="error", us_status="unknown",
                       eu_status="unknown", rights_overall="unknown",
                       rights_confidence="none", verification="unresolved",
                       needs_review="yes", review_reason=str(exc))
            recs = []
        local_lines.append(f"[row time] {time.time() - t0:.1f}s")
        return local_lines, row, recs

    t0 = time.time()
    total = len(work)
    with futures.ThreadPoolExecutor(max_workers=ROW_WORKERS) as pool:
        jobs = {}
        for i, value in work[BOOK_COLUMN].items():
            language = clean(work.loc[i].get("Language"))
            iso = clean(work.loc[i].get("ISO"))
            jobs[pool.submit(run_one, int(i), value, language, iso)] = int(i)
        done = 0
        for fut in futures.as_completed(jobs):
            idx = jobs[fut]
            lines, row, recs = fut.result()
            trace.block(lines)
            trace(f"[{done + 1}/{total}] row {idx} done")
            rows_by_index[idx] = row
            per_book.extend(recs)
            done += 1
            if done % 10 == 0 or done == total:
                http.save()
                trace(f"[progress] {done}/{total} rows, {time.time() - t0:.0f}s, "
                      f"gb calls={providers.gb_calls}")
                _write(work, rows_by_index, per_book, args)

    http.save()
    out = _write(work, rows_by_index, per_book, args)
    trace(f"wrote {args.output}")
    trace(f"wrote {args.per_book_output}")
    trace(f"wrote {SUMMARY_FILE}")
    trace(f"Google Books requests: {providers.gb_calls}")

    pb = pd.DataFrame(per_book)
    if not pb.empty:
        trace("status counts (overall): " + ", ".join(
            f"{k}={v}" for k, v in
            pb["rights_overall"].value_counts().items()))
        trace("needs review: " + str((pb["needs_review"] == "yes").sum())
              + "/" + str(len(pb)))
    return 0


def _write(work, rows_by_index, per_book, args):
    idx = [i for i in work.index if i in rows_by_index]
    if not idx:
        return None
    base = work.loc[idx].reset_index(drop=True)
    extra = pd.DataFrame([rows_by_index[i] for i in idx]).reset_index(drop=True)
    out = pd.concat([base, extra], axis=1)
    out.index = idx
    for col in ("parsed_year", "parsed_year_original", "year_used",
                "matched_year", "author_death_year"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
    out.to_csv(args.output, index=False)

    pb = pd.DataFrame(per_book)
    if not pb.empty:
        pb.sort_values(["row_index", "book_no"]).to_csv(
            args.per_book_output, index=False)
    write_summary(out, pb, Path(args.log))
    return out


if __name__ == "__main__":
    sys.exit(main())
