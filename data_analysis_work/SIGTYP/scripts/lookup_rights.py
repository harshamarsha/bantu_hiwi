#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lookup_rights.py
================

For every row of ``sigtyp_sub_dicts.csv`` this script

1. parses the free-text ``Book`` cell into (author, title, year, publisher) —
   a cell that lists several links is parsed as several separate books and
   each one gets its own verdict,
2. looks the book up in Google Books / Open Library / Crossref / Internet
   Archive (identity and licence evidence only -- the catalogue year is
   *never* used as the publication year),
3. resolves the first author's death year from **VIAF** first and **Wikipedia**
   second (no other source),
4. decides the copyright / public-domain status **in the USA** and **in the
   European Union** from the applicable statutory rules, and
5. writes

   * ``sigtyp_sub_dicts_with_rights.csv``  -- the input table + new columns
   * ``sigtyp_sub_dicts_rights_per_book.csv`` -- one line per book in a cell
   * ``rights_lookup.log``                 -- a human readable trace of every
                                              parse, query, match and decision
   * ``rights_lookup_cache.json``          -- HTTP cache (re-runs are free,
                                              ``--offline`` works)
   * ``author_deaths_overrides.csv``       -- template to hand-curate death
                                              years the resolver could not find

Publication year: strict rule
-----------------------------
The publication year used for every rights verdict is the one written in the
``Book`` cell (a citation year or the year embedded in a PDF file name/URL).
Catalogue years are kept in ``lookup_year`` for inspection only; if the cell
has no year the verdict is ``unknown`` even when a catalogue suggests one.

Author death year: VIAF then Wikipedia
--------------------------------------
Only two sources are consulted: ``viaf.org`` (personal-name search, the
cluster's ``deathDate``) and the English Wikipedia API (a matching article's
``YYYY deaths`` category, else the life dates in the first sentence). The
higher-confidence VIAF answer wins if the two disagree; the disagreement is
written to the log and to ``rights_notes``. Hand-written values in
``author_deaths_overrides.csv`` override both.

Usage
-----
    python3 scripts/lookup_rights.py                      # full run (~130 rows)
    python3 scripts/lookup_rights.py --limit 5            # smoke test on 5 rows
    python3 scripts/lookup_rights.py --offline            # cache only, no network
    python3 scripts/lookup_rights.py --as-of-year 2027    # recompute for another year

Optional
--------
    export GOOGLE_BOOKS_API_KEY=...   # keyless Google Books is quota-limited
    isbnlib (pip install isbnlib) is used automatically *if* an ISBN is
    discovered for a book; the source table itself contains no ISBNs, so this
    path is normally unused.

Why not just isbnlib?
----------------------
isbnlib is an ISBN-centric client: it cannot search by title+author+year and
the ``Book`` column has no ISBN. The ISBN path is still implemented (as an
optional cross-check once Open Library/Google Books report an ISBN), but the
real work is done by the title/author/year search providers below.

Jurisdiction rules implemented (see RULES in the log header)
------------------------------------------------------------
USA  (17 U.S.C. §§302-305)
    * published <= as_of_year - 96  (1930 for 2026) -> public domain
      (95-year term from publication has expired)
    * 1931 .. 1977 -> presumed in copyright, term = publication + 95 years.
      (A US work of 1931-1963 could be public domain if the copyright was not
      renewed; that requires a renewal-database look-up and is flagged, not
      guessed.)
    * >= 1978 -> in copyright
EU   (Directive 2006/116/EC, art. 1)
    * known author death year: term = death + 70 years, running from 1 January
      of the following year -> public domain if death <= as_of_year - 71
      (1955 for 2026)
    * unknown death year, anonymous/corporate author: term = publication + 70
    * unknown death year, named author: publication year only gives an upper
      bound.  A work published after as_of_year-71 cannot be public domain
      unless the author died before publication (posthumous), so it is marked
      "presumed_in_copyright"; older works are marked
      "presumed_public_domain" with an explicit caveat.
    * multi-author works: the term runs from the death of the *last surviving*
      author, therefore PD can only be asserted if every author is resolved.

Every status therefore records its *basis* string, and the confidence column
says whether the year came from the citation itself, from an external record,
or not at all.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures as futures
import difflib
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import pandas as pd
import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

AS_OF_YEAR = 2026
US_TERM_YEARS = 95          # term for works published 1923-1977
EU_TERM_YEARS = 70          # life + 70

HERE = Path(__file__).resolve().parent          # .../SIGTYP/scripts
ROOT = HERE.parent                              # .../SIGTYP
INPUT_CSV = str(ROOT / "sigtyp_sub_dicts.csv")
OUTPUT_CSV = str(ROOT / "sigtyp_sub_dicts_with_rights.csv")
PER_BOOK_CSV = str(ROOT / "sigtyp_sub_dicts_rights_per_book.csv")
LOG_FILE = str(ROOT / "rights_lookup.log")
CACHE_FILE = str(ROOT / "rights_lookup_cache.json")
OVERRIDES_FILE = str(ROOT / "author_deaths_overrides.csv")

BOOK_COLUMN = "Book"

# polite politeness: minimum seconds between two requests to the same host
HOST_INTERVAL = {
    "www.googleapis.com": 0.4,
    "openlibrary.org": 0.35,
    "api.crossref.org": 0.25,
    "archive.org": 0.25,
    "viaf.org": 0.5,
    "en.wikipedia.org": 0.4,
}
DEFAULT_INTERVAL = 0.3
MAX_WORKERS = 4
ROW_WORKERS = 3          # rows processed in parallel (log is buffered per row)

# words that tell us a parenthesised chunk is a series note, not the title
SERIES_HINTS = (
    "series", "dictionar", "lexicon", "lexique", "publ", "lot", "aap",
    "langues", "linguist", "sprache", "annalen", "reeks", "afrikanist",
    "studies", "monograph", "working papers", "arbeitspapiere", "zeitschrift",
    "bulletin", "journal", "cahiers", "revue", "edicion", "edition",
)

# words that mark the start of an imprint statement
IMPRINT_RE = re.compile(
    r"\b(?:press|verlag|publ|editions?|impr|university|universit|bookshop|"
    r"mission|institut|inst|sil|lincom|fountain|brill|mouton|kegan|longman|"
    r"macmillan|clarendon|dept|centre|center|society|soc|ministry|project|"
    r"akademie|academic|druck|books|publishing|house|assoc|ass|college|"
    r"school|seminary|bureau|office|selaf|karthala|peeters|harrassowitz|"
    r"dietrich|rudiger|köppe|koppe|riedel|reimer)\b",
    re.IGNORECASE,
)

ABBREV = {
    "pp", "p", "ed", "eds", "edn", "vol", "no", "dept", "univ", "inst", "ass",
    "soc", "ltd", "co", "inc", "impr", "pub", "publ", "st", "mt", "dr", "prof",
    "rev", "fig", "ca", "cf", "al", "jr", "sr", "vs", "approx", "etc", "b",
    "d", "r", "j", "h", "e", "mr", "mrs", "ms", "gen", "col", "lt", "sgt",
}

# leading junk in undated citations: [19??d], 19xx., n.d., [Anonymous].
LEADING_NOISE_RE = re.compile(
    r"^\s*(?:\[[^\]]{0,22}\]|\([^)]{0,22}\)|"
    r"n\.?\s?d\.?(?=\s|$|[.,;:])|s\.?\s?d\.?(?=\s|$|[.,;:])|"
    r"anonymous\]?\.?(?=\s|$|[.,;:])|"
    r"\[?\s*(?:18|19|20)(?:\d{0,1}[?x–—\-]{1,2}[a-z]?)\s*\]?)\s*[.,;:]?\s*",
    re.IGNORECASE,
)

PERSON_HINTS = (
    "linguist", "anthropolog", "ethnolog", "missionar", "priest", "bishop",
    "writer", "author", "translator", "professor", "philolog", "orientalist",
    "clergyman", "pastor", "explorer", "colonial", "physician", "teacher",
    "botanist", "historian", "folklorist", "lexicographer", "publisher",
    "africanist", "editor", "poet", "journalist", "diplomat", "politician",
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

class RowLog:
    """Per-row buffer so that parallel rows do not interleave in the log."""

    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str = "", indent: int = 0) -> None:
        self.lines.append(("    " * indent) + msg)

    def rule(self, char: str = "=") -> None:
        self.lines.append(char * 88)


class Trace:
    """Writes the same lines to the log file and (optionally) the console."""

    def __init__(self, path: Path, echo: bool = True):
        self.path = path
        self.echo = echo
        self._fh = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self.t0 = time.time()

    def __call__(self, msg: str = "", indent: int = 0) -> None:
        line = ("    " * indent) + msg
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            if self.echo:
                print(line)

    def block(self, lines: list[str]) -> None:
        with self._lock:
            self._fh.write("\n".join(lines) + "\n")
            self._fh.flush()
            if self.echo:
                print("\n".join(lines))

    def rule(self, char: str = "=") -> None:
        self(char * 88)

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- #
# HTTP with cache, retries, per-host rate limiting
# --------------------------------------------------------------------------- #

class ProviderUnavailable(Exception):
    pass


class Http:
    def __init__(self, cache_path: Path, offline: bool, trace: Trace):
        self.cache_path = cache_path
        self.offline = offline
        self.trace = trace
        self._cache: dict[str, Any] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.trace(f"[warn] cache {cache_path} unreadable, starting empty")
        self._lock = threading.Lock()
        self._host_next = {}
        self._local = threading.local()
        self.stats = {"hit": 0, "miss": 0, "error": 0}
        self._dirty = False

    # -- cache -------------------------------------------------------------
    @staticmethod
    def _key(url: str, params: dict | None) -> str:
        blob = url + "?" + json.dumps(params or {}, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def save(self) -> None:
        if self._dirty:
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)

    # -- session / throttling ---------------------------------------------
    def _session(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            s = requests.Session()
            s.headers.update({
                "User-Agent": "sigtyp-rights-lookup/1.0 (research use; contact: local)",
                "Accept": "application/json",
            })
            self._local.s = s
        return self._local.s

    def _throttle(self, host: str) -> None:
        interval = HOST_INTERVAL.get(host, DEFAULT_INTERVAL)
        with self._lock:
            now = time.time()
            wait = self._host_next.get(host, 0.0) - now
            self._host_next[host] = max(now, self._host_next.get(host, 0.0)) + interval
        if wait > 0:
            time.sleep(wait)

    # -- main request ------------------------------------------------------
    def get_json(self, url: str, params: dict | None = None,
                 timeout: int = 20) -> Any:
        key = self._key(url, params)
        with self._lock:
            if key in self._cache:
                self.stats["hit"] += 1
                return json.loads(json.dumps(self._cache[key]))
        if self.offline:
            self.stats["miss"] += 1
            return None

        host = urlparse(url).netloc
        last_err = ""
        for attempt in range(3):
            self._throttle(host)
            try:
                r = self._session().get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    last_err = "invalid JSON"
                    break
                with self._lock:
                    self._cache[key] = data
                    self._dirty = True
                    self.stats["miss"] += 1
                return data
            if r.status_code in (429, 403):
                retry = r.headers.get("Retry-After")
                wait = float(retry) if (retry or "").isdigit() else 2.0 * (attempt + 1)
                last_err = f"HTTP {r.status_code}"
                if attempt < 2:
                    time.sleep(wait)
                    continue
                raise ProviderUnavailable(last_err)
            if r.status_code == 404:
                with self._lock:
                    self._cache[key] = {"__notfound__": True}
                    self._dirty = True
                return None
            if 500 <= r.status_code < 600:
                last_err = f"HTTP {r.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            last_err = f"HTTP {r.status_code}"
            break

        self.stats["error"] += 1
        self.trace(f"[http] failed {url} ({last_err})")
        return None


# --------------------------------------------------------------------------- #
# Parsed representation of a Book cell
# --------------------------------------------------------------------------- #

@dataclass
class ParsedBook:
    kind: str                       # citation|url|archive_item|filename|bibkey|junk|empty
    authors: list[str] = field(default_factory=list)
    author_display: str = ""
    title: str = ""
    year: int | None = None
    original_year: int | None = None
    publisher: str = ""
    urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    authors_are_surnames: bool = False   # True for names mined from file names

    @staticmethod
    def _surname(a: str) -> str:
        a = clean(a)
        if not a or a.lower().strip("[]") in ("anonymous", "anon"):
            return ""
        if "," in a:
            return clean(a.split(",")[0])
        return a

    @property
    def query_author(self) -> str:
        """Surnames only, joined -- used in search queries."""
        names = []
        for a in self.authors:
            if self.authors_are_surnames:
                s = clean(a)
            elif "," in a:
                s = clean(a.split(",")[0])
            else:
                toks = [t for t in a.split()
                        if t.lower() not in ("van", "von", "de", "der", "da", "di",
                                             "du", "le", "la", "el")]
                s = toks[-1] if toks else a
            if s and s.lower() not in ("anonymous", "anon"):
                names.append(s)
        # de-duplicate while keeping order
        seen, out = set(), []
        for n in names:
            if n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return " ".join(out[:3])

    @property
    def full_author(self) -> str:
        return self.author_display or ", ".join(self.authors)

    @property
    def person_name(self) -> str:
        """First author in 'Given Surname' order for people search engines."""
        if not self.authors:
            return ""
        return person_name_from(self.authors[0])


def person_name_from(a: str) -> str:
    a = clean(a)
    if not a or a.lower().strip("[]") in ("anonymous", "anon"):
        return ""
    if "," in a:
        surname, _, given = a.partition(",")
        given = re.sub(r"\b(?:van|von|de|der|da|di|du|le|la)\b", "", given,
                       flags=re.IGNORECASE)
        return clean(f"{given} {surname}")
    return a


YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d{2}|20[0-2]\d)(?!\d)")
BRACKET_YEAR_RE = re.compile(r"\[\s*(1[5-9]\d{2}|20[0-2]\d)\s*\]")
PAGE_RE = re.compile(
    r"\s*(?:(?:[ivxlcdm]+|\d+)\s*\+\s*)?(?:\d+\s*)?pp?\.?\s*$", re.IGNORECASE
)


def norm(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
    return text.strip(" .,;:)]}[({")


def _strip_pages(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = PAGE_RE.sub("", s).strip()
    return s


def _drop_series_parens(text: str) -> str:
    """Remove balanced parenthesised series notes, e.g. '(LoT (Langues ...) 27)'."""
    out, i = [], 0
    while i < len(text):
        if text[i] != "(":
            out.append(text[i])
            i += 1
            continue
        depth, j = 0, i
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        chunk = text[i:j + 1]
        inner = norm(chunk)
        if any(h in inner for h in (norm(h) for h in SERIES_HINTS)) or re.search(r"\d", chunk):
            i = j + 1
            # swallow a following stray space
            if i < len(text) and text[i] == " ":
                i += 1
        else:
            out.append(chunk)
            i = j + 1
    return "".join(out)


def _split_sentences(text: str) -> list[str]:
    """Split on '. ' but not after initials/abbreviations ('Dept.', 'pp.')."""
    out, start = [], 0
    for m in re.finditer(r"\.\s+(?=[A-ZÀ-Ý0-9\[(“])", text):
        before = text[start:m.start()]
        prev = before.split()[-1].strip(".").lower() if before.split() else ""
        if prev in ABBREV or len(prev) == 1:
            continue
        out.append(text[start:m.start() + 1])
        start = m.end()
    out.append(text[start:])
    return [s for s in (clean(x) for x in out) if s]


def _strip_leading_noise(text: str) -> tuple[str, list[str]]:
    notes = []
    prev = None
    while prev != text:
        prev = text
        m = LEADING_NOISE_RE.match(text)
        if m:
            notes.append(f"ignored leading {text[:m.end()].strip()!r}")
            text = text[m.end():]
    return text, notes


def _title_and_imprint(rest: str) -> tuple[str, str]:
    """Split the post-year remainder into (title, imprint/publisher text)."""
    rest = _drop_series_parens(rest)
    rest = _strip_pages(rest)
    rest = re.sub(r"\s*\b\d+(?:st|nd|rd|th)\s+edn?\.?\s*", " ", rest, flags=re.IGNORECASE)
    rest = clean(rest)
    sents = _split_sentences(rest)
    if not sents:
        return "", ""

    series_sent = re.compile(
        r"\b(?:series|monographs?|arbeitspapiere|zeitschrift|bulletin|journal|"
        r"cahiers|revue|working papers|annalen|reeks|publ\.|publications)\b",
        re.IGNORECASE)

    title_parts, imprint = [sents[0]], ""
    for idx, sent in enumerate(sents[1:], start=1):
        if re.fullmatch(r"\d+[-–—]\d+|\d+", sent):
            continue                      # bare page range between title and imprint
        if series_sent.search(sent) and ":" not in sent:
            continue                      # series statement, not a publisher
        remainder = " ".join(sents[idx:])
        if ":" in remainder[: max(80, len(sent) + 2)] or IMPRINT_RE.search(sent):
            imprint = remainder
            break
        title_parts.append(sent)

    return clean(" ".join(title_parts)), clean(imprint)


def _publisher_from_imprint(imprint: str) -> tuple[str, str]:
    """'Lyon: Impr. E. Vitte' -> ('Lyon', 'Impr. E. Vitte')."""
    if not imprint:
        return "", ""
    imp = clean(imprint)
    if ":" in imp:
        place, _, pub = imp.partition(":")
        place = clean(place)
        if 0 < len(place) < 60:
            return place, clean(pub)
    return "", imp


def parse_citation(raw: str) -> ParsedBook:
    """author-year (or undated) citation -> ParsedBook."""
    p = ParsedBook(kind="citation")
    text, noise = _strip_leading_noise(clean(raw))
    p.notes += noise

    br = BRACKET_YEAR_RE.search(text)
    if br:
        p.original_year = int(br.group(1))

    m = YEAR_RE.search(text)
    if not m:
        p.notes.append("no publication year in the citation")
        title, imprint = _title_and_imprint(text)
        p.title = title
        place, pub = _publisher_from_imprint(imprint)
        p.publisher = pub
        if place:
            p.notes.append(f"place={place}")
        return p

    p.year = int(m.group(1))

    head = clean(text[:m.start()]).strip(" .(")
    head = BRACKET_YEAR_RE.sub("", head)
    head = re.sub(r"\[\s*(?:18|19|20)[\d?–-]{2}\s*[a-z]?\]", "", head)
    head, more_noise = _strip_leading_noise(clean(head).strip(" .,"))
    p.notes += more_noise
    if head:
        p.author_display = head
        p.authors = [clean(a) for a in re.split(r"\s*(?:&|\band\b|;)\s*", head) if clean(a)]

    rest = text[m.end():]
    rest = re.sub(r"^[\s\.\)\]\]:,]+", "", rest)
    rest = BRACKET_YEAR_RE.sub("", rest)
    p.title, imprint = _title_and_imprint(rest)
    place, pub = _publisher_from_imprint(imprint)
    p.publisher = pub
    if place:
        p.notes.append(f"place={place}")
    return p


def parse_pdf_name(name: str) -> ParsedBook:
    """'helmlinger_duala1972.pdf' -> author helmlinger, hint 'duala', 1972."""
    p = ParsedBook(kind="url", authors_are_surnames=True)
    stem = unquote(name)
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)

    br = BRACKET_YEAR_RE.search(stem)
    if br:
        p.original_year = int(br.group(1))
        stem = stem[:br.start()] + stem[br.end():]

    years = list(YEAR_RE.finditer(stem))
    if not years:
        p.notes.append("no 4-digit year in file name")
        p.title = clean(stem.replace("_", " ").replace("-", " "))
        return p

    ym = years[-1]
    p.year = int(ym.group(1))
    head, tail = stem[:ym.start()], stem[ym.end():]
    if re.search(r"\d", tail):
        p.notes.append(f"ignored trailing '{tail}'")
    if re.search(r"v\d|[_-][os]$", tail):
        p.notes.append(f"variant suffix '{tail}'")

    if "_" in head:
        author_raw, _, title_raw = head.partition("_")
    else:
        author_raw, title_raw = head, ""

    author_raw = re.sub(r"(?:etal|et al|and)$", "", author_raw, flags=re.IGNORECASE)
    author_raw = author_raw.strip("_-")
    p.authors = [clean(x) for x in author_raw.split("-") if clean(x)]
    p.author_display = " ".join(p.authors)

    hint = clean(title_raw.replace("_", " ").replace("-", " "))
    hint = re.sub(r"\b(?:dictionary|dictionnaire|vocabulary|vocabulaire|lexique|lexicon|"
                  r"grammar|handbook)\b", " ", hint, flags=re.IGNORECASE)
    p.title = clean(hint)
    if not p.title:
        p.notes.append("file name has no title/language hint")
    return p


def parse_cell(value: Any) -> ParsedBook:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ParsedBook(kind="empty", notes=["empty Book cell"])
    raw = str(value).strip()
    if not raw or raw in {"-", "–", "—", "nan", "NaN", "None"}:
        return ParsedBook(kind="empty", notes=[f"placeholder value {raw!r}"])

    # 1. archive.org item page ------------------------------------------------
    m = re.search(r"archive\.org/details/([A-Za-z0-9._-]+)", raw)
    if m:
        p = ParsedBook(kind="archive_item", urls=[raw])
        p.notes.append(f"internet-archive identifier={m.group(1)}")
        p.title = m.group(1)
        return p

    # 2. one or more PDF urls / bare file names -------------------------------
    urls = re.findall(r"https?://\S+", raw)
    if urls:
        p = ParsedBook(kind="url", urls=urls)
        names = [unquote(Path(urlparse(u).path).name) for u in urls]
        names = [n for n in names if n.lower().endswith(".pdf")] or names
        parsed = [parse_pdf_name(n) for n in names if n]
        if parsed:
            best = parsed[0]
            hints, seen = [], set()
            for x in parsed:
                if x.title and x.title.lower() not in seen:
                    seen.add(x.title.lower())
                    hints.append(x.title)
            p.authors = best.authors
            p.author_display = best.author_display
            p.authors_are_surnames = True
            p.year, p.original_year = best.year, best.original_year
            p.title = best.title
            p.notes += best.notes
        if len(urls) > 1:
            listing = "; ".join(
                f"{x.query_author or '?'} {x.year or '????'}" for x in parsed)
            p.notes.append(f"{len(urls)} books in this cell; each is judged "
                           f"separately (parse_cell_all); all: {listing}")
            if len(hints) > 1:
                p.notes.append("language/title hints in cell: " + ", ".join(hints))
        return p

    if raw.lower().endswith(".pdf"):
        p = parse_pdf_name(raw)
        p.kind = "filename"
        p.urls = []
        p.notes.append("bare file name, not a url")
        return p
    m = re.search(r"\(\s*([A-Za-z0-9._\-]+\.pdf)\s*\)", raw, re.IGNORECASE)
    if m:
        p = parse_pdf_name(m.group(1))
        p.kind = "filename"
        p.notes.append(f"file name extracted from prose: {m.group(1)}")
        return p

    # 3. bibtex-ish dict of database keys: {130338: 'NaN', 587723: '164'} -----
    if raw.startswith("{") and raw.endswith("}"):
        p = ParsedBook(kind="bibkey")
        try:
            d = ast.literal_eval(raw)
            p.notes.append(f"{len(d)} internal bibliography key(s), pages="
                           f"{sorted(set(map(str, d.values())))[:4]}")
        except (ValueError, SyntaxError):
            p.notes.append("brace-delimited cell could not be parsed")
        p.notes.append("internal database key: no title/author/year to look up")
        return p

    # 4. ordinary citation (dated or undated) --------------------------------
    p = parse_citation(raw)
    if not p.title and not p.authors and not p.year:
        p.kind = "junk"
        p.notes.append("no usable author/year information")
    return p


def parse_cell_all(value: Any) -> list[ParsedBook]:
    """Every *distinct book* written in one Book cell.

    A cell often carries several links (one dictionary per line, sometimes with
    a language hint after the link).  Each link is a separate work with its own
    author, year and therefore its own rights verdict, so it is parsed on its
    own.  Duplicate scans of the same work (an ``_o``/``v2`` OCR copy next to
    the plain file name) collapse to a single book.  A cell without links holds
    at most one work.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return [parse_cell(value)]
    urls = re.findall(r"https?://\S+", str(value))
    if len(urls) <= 1:
        return [parse_cell(value)]

    books: list[ParsedBook] = []
    seen: set[tuple] = set()
    for u in urls:
        book = parse_cell(u)
        key = (norm(book.query_author), book.year, norm(book.title),
               book.original_year or 0)
        if key in seen:                       # same work, second scan/copy
            continue
        seen.add(key)
        book.notes.append(f"book {len(books) + 1} in this cell "
                          f"({Path(urlparse(u).path).name})")
        books.append(book)
    return books or [parse_cell(value)]


# --------------------------------------------------------------------------- #
# Lookup providers
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    source: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    publisher: str = ""
    isbn: str = ""
    public_domain: bool | None = None
    access: str = ""
    license: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict)


class Providers:
    def __init__(self, http: Http, trace: Trace):
        self.http = http
        self.trace = trace
        self.gb_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
        self.gb_dead = False
        self._gb_lock = threading.Lock()
        self._gb_failures = 0

    # -- Google Books ------------------------------------------------------
    def google_books(self, query: str, limit: int = 5,
                     parsed: ParsedBook | None = None) -> list[Candidate]:
        if self.gb_dead:
            raise ProviderUnavailable("disabled earlier in this run (quota/403)")
        q = query
        if parsed is not None and parsed.title:
            q = f'intitle:"{short_title(parsed.title)}"'
            if parsed.query_author:
                q += f" inauthor:{parsed.query_author.split()[0]}"
        params = {"q": q, "maxResults": limit, "printType": "books"}
        if self.gb_key:
            params["key"] = self.gb_key
        try:
            data = self.http.get_json("https://www.googleapis.com/books/v1/volumes", params)
        except ProviderUnavailable as exc:
            with self._gb_lock:
                self.gb_dead = True
            self.trace("[google-books] disabled for the rest of this run "
                       f"({exc}): continuing with Open Library/Crossref/IA "
                       "(set GOOGLE_BOOKS_API_KEY to enable)")
            raise ProviderUnavailable(str(exc))
        out = []
        for item in (data or {}).get("items", []):
            vi = item.get("volumeInfo", {})
            ai = item.get("accessInfo", {})
            ids = {x.get("type"): x.get("identifier") for x in vi.get("industryIdentifiers", [])}
            year = None
            m = YEAR_RE.search(vi.get("publishedDate", "") or "")
            if m:
                year = int(m.group(1))
            out.append(Candidate(
                source="google_books",
                title=vi.get("title", "") + (
                    ": " + vi["subtitle"] if vi.get("subtitle") else ""),
                authors=vi.get("authors", []) or [],
                year=year,
                publisher=vi.get("publisher", "") or "",
                isbn=ids.get("ISBN_13") or ids.get("ISBN_10") or "",
                public_domain=ai.get("publicDomain"),
                access=ai.get("viewability", "") or "",
                url=vi.get("infoLink", "") or "",
                extra={"textSnippet": item.get("searchInfo", {}).get("textSnippet", "")},
            ))
        return out

    # -- Open Library ------------------------------------------------------
    def openlibrary(self, query: str, limit: int = 5,
                    parsed: ParsedBook | None = None) -> list[Candidate]:
        fields = ("title,author_name,first_publish_year,publisher,isbn,key,"
                  "public_scan_b,ia,ebook_access,edition_count")
        params: dict[str, Any] = {"limit": limit, "fields": fields}
        if parsed is not None and parsed.title:
            params["title"] = short_title(parsed.title)
            if parsed.query_author:
                params["author"] = parsed.query_author.split()[0]
        else:
            params["q"] = query
        data = self.http.get_json("https://openlibrary.org/search.json", params)
        docs = (data or {}).get("docs", [])
        if not docs and "q" not in params:
            # fielded search is precise but strict: retry as free text once
            data = self.http.get_json("https://openlibrary.org/search.json",
                                      {"q": query, "limit": limit, "fields": fields})
            docs = (data or {}).get("docs", [])
        out = []
        for doc in docs:
            out.append(Candidate(
                source="openlibrary",
                title=doc.get("title", "") or "",
                authors=doc.get("author_name", []) or [],
                year=doc.get("first_publish_year"),
                publisher="; ".join((doc.get("publisher") or [])[:2]),
                isbn=(doc.get("isbn") or [""])[0],
                public_domain=True if doc.get("public_scan_b") else None,
                access=doc.get("ebook_access", "") or "",
                url="https://openlibrary.org" + doc.get("key", ""),
                extra={"ia": (doc.get("ia") or [])[:2]},
            ))
        return out

    # -- Crossref ----------------------------------------------------------
    def crossref(self, query: str, limit: int = 5) -> list[Candidate]:
        data = self.http.get_json(
            "https://api.crossref.org/works",
            {"query.bibliographic": query, "rows": limit,
             "select": "title,author,issued,publisher,ISBN,type,DOI,license"},
        )
        out = []
        for item in (data or {}).get("message", {}).get("items", []):
            title = (item.get("title") or [""])[0]
            year = None
            parts = (item.get("issued", {}) or {}).get("date-parts", [[None]])
            if parts and parts[0] and parts[0][0]:
                year = int(parts[0][0])
            authors = []
            for a in item.get("author", []) or []:
                nm = " ".join(x for x in (a.get("given"), a.get("family")) if x)
                if nm:
                    authors.append(nm)
            lic = ""
            for l in item.get("license", []) or []:
                lic = l.get("URL", "")
                break
            out.append(Candidate(
                source="crossref",
                title=title,
                authors=authors,
                year=year,
                publisher=item.get("publisher", "") or "",
                isbn=(item.get("ISBN") or [""])[0],
                access="metadata_only",
                license=lic,
                url=("https://doi.org/" + item["DOI"]) if item.get("DOI") else "",
                extra={"type": item.get("type", "")},
            ))
        return out

    # -- Internet Archive search ------------------------------------------
    def internet_archive(self, query: str, limit: int = 5) -> list[Candidate]:
        data = self.http.get_json(
            "https://archive.org/advancedsearch.php",
            {"q": query, "rows": limit, "output": "json",
             "fl[]": ["identifier", "title", "creator", "year", "date",
                      "publisher", "licenseurl", "collection",
                      "access-restricted-item"]},
        )
        out = []
        for doc in (data or {}).get("response", {}).get("docs", []):
            coll = doc.get("collection") or []
            if isinstance(coll, str):
                coll = [coll]
            lic = doc.get("licenseurl") or ""
            year = None
            if doc.get("year"):
                try:
                    year = int(str(doc["year"])[:4])
                except ValueError:
                    year = None
            if year is None and doc.get("date"):
                m = YEAR_RE.search(str(doc["date"]))
                year = int(m.group(1)) if m else None
            pd = ("publicdomain" in coll) or ("public_domain" in lic.lower()) or \
                 ("creativecommons.org/publicdomain" in lic.lower()) or None
            creator = doc.get("creator") or []
            if isinstance(creator, str):
                creator = [creator]
            out.append(Candidate(
                source="internet_archive",
                title=doc.get("title", "") or "",
                authors=creator,
                year=year,
                publisher=doc.get("publisher", "") or (doc.get("publisher") or ""),
                public_domain=pd,
                access="restricted" if doc.get("access-restricted-item") in (True, "true") else "open",
                license=lic,
                url="https://archive.org/details/" + str(doc.get("identifier", "")),
                extra={"collection": coll[:4]},
            ))
        return out

    # -- Internet Archive single item -------------------------------------
    def ia_item(self, identifier: str) -> list[Candidate]:
        data = self.http.get_json(f"https://archive.org/metadata/{identifier}")
        if not data or not data.get("metadata"):
            return []
        m = data["metadata"]
        year = None
        for fieldname in ("date", "year", "publicdate"):
            if m.get(fieldname):
                mm = YEAR_RE.search(str(m[fieldname]))
                if mm:
                    year = int(mm.group(1))
                    break
        coll = m.get("collection") or []
        if isinstance(coll, str):
            coll = [coll]
        lic = m.get("licenseurl") or ""
        creator = m.get("creator") or []
        if isinstance(creator, str):
            creator = [creator]
        return [Candidate(
            source="internet_archive_item",
            title=m.get("title", "") or "",
            authors=creator,
            year=year,
            publisher=m.get("publisher", "") or "",
            public_domain=("publicdomain" in coll) or ("publicdomain" in lic.lower()) or None,
            access="restricted" if str(m.get("access-restricted-item", "")).lower() == "true" else "open",
            license=lic,
            url="https://archive.org/details/" + identifier,
            extra={"collection": coll[:4], "identifier": identifier},
        )]

    # -- author death year: VIAF, then Wikipedia --------------------------
    def person_death(self, name: str, year: int | None = None) -> dict | None:
        """Resolve a death year from VIAF and/or Wikipedia (only these two)."""
        viaf = self._viaf_death(name, year)
        wiki = self._wikipedia_death(name, year)
        if _implausible_lifetime(viaf, year):
            viaf = None
        if _implausible_lifetime(wiki, year):
            wiki = None
        if not viaf and not wiki:
            return None
        chosen = dict(viaf or wiki)
        chosen["viaf"] = viaf
        chosen["wikipedia"] = wiki
        if viaf and wiki and viaf.get("death") and wiki.get("death") \
                and viaf["death"] != wiki["death"]:
            chosen["conflict"] = (f"VIAF says {viaf['death']}, Wikipedia says "
                                  f"{wiki['death']}; VIAF used")
        return chosen

    def _viaf_death(self, name: str, year: int | None) -> dict | None:
        safe = re.sub(r'["\\]', " ", name)
        data = self.http.get_json(
            "https://viaf.org/viaf/search",
            {"query": f'local.personalNames all "{safe}"',
             "maximumRecords": 20},
        )
        resp = (data or {}).get("searchRetrieveResponse") or {}
        recs = (resp.get("records") or {}).get("record") or []
        if isinstance(recs, dict):
            recs = [recs]
        best, best_rank = None, None
        for rec in recs:
            rd = rec.get("recordData") or {}
            cluster = next((v for k, v in rd.items()
                            if k.endswith("VIAFCluster") and isinstance(v, dict)),
                           None)
            if not cluster:
                continue
            death = _first_year(_find_key_values(cluster, "deathDate"))
            if not death:
                continue
            birth = _first_year(_find_key_values(cluster, "birthDate"))
            labels = _viaf_labels(cluster)
            matching = [l for l in labels if person_name_matches(name, l)]
            if not matching:
                continue
            label = max(matching, key=lambda l: name_similarity(name, l))
            sim = name_similarity(name, label)
            # a person born after the book appeared, too young to have written
            # it, or dead long before it, cannot be the author: prefer a
            # plausible cluster when one exists
            plausible = not ((year and birth and not (15 <= year - birth <= 100)) or
                             (year and death and death < year - 5))
            rank = (1 if plausible else 0, sim)
            if best is None or rank > best_rank:
                best_rank = rank
                best = {
                    "source": "viaf",
                    "id": str(_first_scalar(_find_key_values(cluster, "viafID")) or ""),
                    "label": label,
                    "description": "VIAF personal name authority record",
                    "birth": birth,
                    "death": death,
                    "label_sim": round(sim, 2),
                    "plausible": plausible,
                    "url": "https://viaf.org/viaf/"
                           + str(_first_scalar(_find_key_values(cluster, "viafID")) or ""),
                }
        return best

    def _wikipedia_death(self, name: str, year: int | None) -> dict | None:
        search = self.http.get_json(
            "https://en.wikipedia.org/w/api.php",
            {"action": "query", "list": "search", "srsearch": name,
             "format": "json", "srlimit": 8, "srnamespace": 0},
        )
        hits = ((search or {}).get("query") or {}).get("search") or []
        ranked = []
        for hit in hits:
            title = hit.get("title", "")
            if not wikipedia_title_matches(name, title):
                continue
            snippet = re.sub(r"<[^>]+>", " ", hit.get("snippet", "") or "")
            boost = 0.05 if any(h in snippet.lower() for h in PERSON_HINTS) else 0.0
            ranked.append((name_similarity(name, title) + boost, title, snippet))
        ranked.sort(key=lambda x: -x[0])

        for sim, title, snippet in ranked[:2]:
            page = self.http.get_json(
                "https://en.wikipedia.org/w/api.php",
                {"action": "query", "prop": "categories|extracts",
                 "exintro": 1, "explaintext": 1, "cllimit": "max",
                 "titles": title, "format": "json", "redirects": 1},
            )
            pages = ((page or {}).get("query") or {}).get("pages") or {}
            info = next(iter(pages.values()), {})
            cats = [c.get("title", "") for c in info.get("categories") or []]
            death = birth = None
            for c in cats:
                m = re.search(r"(\d{4})\s+deaths", c)
                if m:
                    death = int(m.group(1))
                    break
            for c in cats:
                m = re.search(r"(\d{4})\s+births", c)
                if m:
                    birth = int(m.group(1))
                    break
            extract = clean(info.get("extract")
                            or re.sub(r"<[^>]+>", " ", snippet or ""))
            if not death:                      # "(1893–1980)" in the lead
                m = re.search(r"\((?:[^()]*?)(\d{4})\s*[–—-]\s*(\d{4})\)", extract)
                if m:
                    birth = birth or int(m.group(1))
                    death = int(m.group(2))
            if not death:
                m = re.search(r"\b(?:died|death)\b[^.]{0,80}?\b(\d{4})\b", extract)
                if m:
                    death = int(m.group(1))
            if not death:
                continue
            if birth and death and not (10 <= death - birth <= 110):
                continue                       # two different people
            if year and birth and birth > year:
                continue                       # born after the book appeared
            return {
                "source": "wikipedia",
                "id": title,
                "label": title,
                "description": (extract[:160] + "...") if extract else "",
                "birth": birth,
                "death": death,
                "label_sim": round(sim, 2),
                "plausible": True,
                "url": "https://en.wikipedia.org/wiki/"
                       + title.replace(" ", "_"),
            }
        return None


# --------------------------------------------------------------------------- #
# Person-name helpers for VIAF / Wikipedia
# --------------------------------------------------------------------------- #

def _find_key_values(obj: Any, suffix: str) -> list:
    """All values of keys ending in *suffix* (XML->JSON keeps ns prefixes)."""
    out: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith(suffix):
                out.append(v)
            out.extend(_find_key_values(v, suffix))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_find_key_values(v, suffix))
    return out


def _first_scalar(values: list) -> Any:
    for v in values:
        if isinstance(v, dict):
            if "content" in v:
                return v["content"]
            continue
        if not isinstance(v, list):
            return v
    return None


def _first_year(values: list) -> int | None:
    for v in values:
        v = _first_scalar([v]) if isinstance(v, dict) else v
        if isinstance(v, (int, float)):
            if 1000 <= int(v) <= 2100:
                return int(v)
            continue
        if isinstance(v, str):
            m = re.search(r"(1[5-9]\d{2}|20[0-2]\d)", v)
            if m:
                return int(m.group(1))
    return None


def _strip_life_dates(label: str) -> str:
    """'Doke, Clement Martyn, 1893-1980' -> 'Doke, Clement Martyn'."""
    return clean(re.sub(r",?\s*\b(?:1[5-9]\d{2}|20[0-2]\d)\b.*$", "", label))


def name_similarity(query: str, label: str) -> float:
    """Order-insensitive similarity of a person name and a heading/title."""
    q = norm(query)
    left = norm(_strip_life_dates(label))
    if not q or not left:
        return 0.0
    qt, lt = q.split(), left.split()
    if not qt or not lt:
        return 0.0
    jaccard = len(set(qt) & set(lt)) / len(set(qt) | set(lt))
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(qt)),
                                     " ".join(sorted(lt))).ratio()
    return max(jaccard, ratio)


def person_name_matches(query: str, label: str) -> bool:
    """Strict enough to reject 'Paul Helmlinger' -> 'Helmlinger, Lars'."""
    qt = norm(query).split()
    lt = norm(_strip_life_dates(label)).split()
    if not qt or not lt or qt[-1] not in lt:      # surname must be present
        return False
    if name_similarity(query, label) >= 0.5:
        return True
    given = [t for t in qt[:-1] if len(t) > 1]      # ignore bare initials
    return bool(given) and all(t in lt for t in given)


def wikipedia_title_matches(query: str, title: str) -> bool:
    """Article titles are noisier than VIAF headings: demand a closer name."""
    qt = norm(query).split()
    lt = norm(title).split()
    if not qt or not lt or qt[-1] not in lt:      # e.g. 'White' vs 'N. C. Wyeth'
        return False
    if name_similarity(query, title) >= 0.75:
        return True
    # otherwise every query token must occur in the title; an initial may be
    # expanded ('J.' -> 'Joseph'), which is how Wikipedia usually titles people
    for t in qt:
        if t in lt or any(u.startswith(t) for u in lt):
            continue
        return False
    return True


def _viaf_labels(cluster: dict) -> list[str]:
    """Personal-name headings such as 'Doke, Clement Martyn, 1893-1980'."""
    raw: list[str] = []
    for heading in _find_key_values(cluster, "mainHeadings"):
        stack = [heading]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.endswith(":text") and isinstance(v, str):
                        raw.append(v)
                    stack.append(v)
            elif isinstance(obj, list):
                stack.extend(obj)
    labels, seen = [], set()
    for t in raw:
        t = clean(t)
        if "," in t and t.lower() not in seen:
            seen.add(t.lower())
            labels.append(t)
    return labels


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def surname_tokens(name: str) -> list[str]:
    name = name.strip()
    if "," in name:
        return [norm(name.split(",")[0])]
    return [norm(t) for t in name.split() if len(t) > 1]


def author_similarity(parsed: ParsedBook, cand: Candidate) -> float:
    if not parsed.authors or not cand.authors:
        return 0.0
    p_sur = {s for a in parsed.authors for s in surname_tokens(a)}
    c_txt = norm(" ".join(cand.authors))
    if not c_txt:
        return 0.0
    hits = sum(1 for s in p_sur if s and s in c_txt)
    if hits == 0:
        return 0.0          # different people: never rescue this with string ratio
    base = hits / max(1, len(p_sur))
    full = difflib.SequenceMatcher(None, norm(parsed.full_author), c_txt).ratio()
    return max(base, full)


def title_similarity(parsed: ParsedBook, cand: Candidate) -> float:
    qt, ct = norm(parsed.title), norm(cand.title)
    if not qt or not ct:
        return 0.0
    ratio = difflib.SequenceMatcher(None, qt, ct).ratio()
    q_tokens = [t for t in qt.split() if len(t) > 2]
    if q_tokens and all(t in ct for t in q_tokens):
        ratio = max(ratio, 0.8)          # "duala" inside "dictionnaire duala-francais"
    return ratio


def score(parsed: ParsedBook, cand: Candidate) -> float:
    t = title_similarity(parsed, cand)
    a = author_similarity(parsed, cand)
    if parsed.year and cand.year:
        dy = abs(parsed.year - cand.year)
        y = 1.0 if dy <= 1 else 0.5 if dy <= 3 else 0.0
    elif parsed.year or cand.year:
        y = 0.3
    else:
        y = 0.0
    s = 0.55 * t + 0.30 * a + 0.15 * y
    # every row of this dataset is a dictionary: when the cell only gives a
    # language hint from a PDF file name, a dictionary-like title is a better fit
    if parsed.kind in ("url", "filename") and re.search(
            r"dictionar|vocabular|lexic|grammar|w[oö]rterbuch|glossar",
            cand.title, re.IGNORECASE):
        s += 0.05
    return round(min(s, 1.0), 3)


SOURCE_PRIORITY = {"google_books": 0, "openlibrary": 1, "internet_archive": 2,
                   "internet_archive_item": 2, "crossref": 3}


def token_coverage(q: str, c: str) -> float:
    q_tokens = [t for t in norm(q).split() if len(t) > 2]
    if not q_tokens:
        return 0.0
    c_norm = norm(c)
    return sum(1 for t in q_tokens if t in c_norm) / len(q_tokens)


def eligible(parsed: ParsedBook, c: Candidate) -> bool:
    """Reject catalogue hits whose author/title do not really match."""
    if not c.title:
        return False
    ts = title_similarity(parsed, c)
    cov = token_coverage(parsed.title, c.title)
    if cov < 0.5 and ts < 0.7:
        return False                 # e.g. 'Unpublished Shisango dictionary'
    if parsed.year and c.year and abs(parsed.year - c.year) > 20 and ts < 0.9:
        return False                 # a different edition/work, e.g. 1904 vs 2025
    if not c.authors and parsed.year and c.year and abs(parsed.year - c.year) > 20:
        return False                 # no author to confirm such a distant year
    if parsed.authors and c.authors:  # vs '0957+561: The unpublished story'
        return author_similarity(parsed, c) >= 0.3 and ts >= 0.35
    if parsed.authors and not c.authors:
        return ts >= 0.9              # no author info: demand a near-exact title
    return ts >= 0.45


def pick_best(parsed: ParsedBook, candidates: list[Candidate],
              trace: Trace) -> tuple[Candidate | None, float]:
    # an archive.org/details/<id> cell identifies one exact item
    if parsed.kind == "archive_item":
        for c in candidates:
            if c.source == "internet_archive_item":
                trace(f"archive.org identifier resolved directly: {c.title} | "
                      f"{', '.join(c.authors[:2])} | {c.year}", indent=3)
                return c, 1.0

    # deterministic ordering: preferred source first for identical records
    candidates = sorted(candidates, key=lambda c: SOURCE_PRIORITY.get(c.source, 9))
    deduped, seen = [], set()
    for c in candidates:
        key = (norm(c.title), c.year)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    scored, rejected = [], 0
    for c in deduped:
        if not eligible(parsed, c):
            rejected += 1
            continue
        scored.append((score(parsed, c), c))
    scored.sort(key=lambda x: -x[0])

    shown = [x for x in scored if x[0] >= 0.2][:8]
    for s, c in shown:
        trace(f"{c.source:22s} {s:5.2f}  {c.year or '????'}  "
              f"{(c.title or '(no title)')[:66]} | {', '.join(c.authors[:2])[:40]}",
              indent=3)
    if rejected:
        trace(f"({rejected} candidate(s) rejected: author mismatch or title too "
              f"different)", indent=3)
    if len(scored) > len(shown):
        trace(f"(+{len(scored) - len(shown)} candidate(s) with score < 0.2 omitted)",
              indent=3)
    if not scored:
        return None, 0.0
    s, c = scored[0]
    if s < 0.55 or title_similarity(parsed, c) < 0.45:
        return None, s
    return c, s


# --------------------------------------------------------------------------- #
# Rights engine
# --------------------------------------------------------------------------- #

def us_rights(year: int | None, as_of: int) -> tuple[str, str]:
    last_pd = as_of - (US_TERM_YEARS + 1)          # 1930 for 2026
    if year is None:
        return "unknown", "no publication year available"
    if year <= last_pd:
        return ("public_domain",
                f"published {year} (<= {last_pd}): the 95-year US term has expired; "
                f"PD in the USA since 1 Jan {year + US_TERM_YEARS + 1}")
    if year <= 1977:
        return ("presumed_in_copyright",
                f"published {year}: term = 95 years from publication "
                f"(expires 1 Jan {year + US_TERM_YEARS + 1}); a US work of 1931-1963 "
                f"could be PD if the copyright was not renewed - renewal/URAA "
                f"status not checked")
    return ("in_copyright",
            f"published {year} (>= 1978): protected in the USA; 95 years from "
            f"publication means PD no earlier than {year + US_TERM_YEARS}")


def eu_rights(year: int | None, death: int | None, n_authors: int,
              as_of: int) -> tuple[str, str]:
    last_pd_death = as_of - (EU_TERM_YEARS + 1)    # 1955 for 2026
    if death:
        expiry = death + EU_TERM_YEARS
        caveat = (" (multi-author: term runs from the death of the last surviving "
                  "author; only the first author was checked)") if n_authors > 1 else ""
        if expiry < as_of:
            return ("public_domain",
                    f"author died {death}: life+70 expired 1 Jan {expiry + 1}{caveat}")
        return ("in_copyright",
                f"author died {death}: protected in the EU until 1 Jan {expiry + 1}{caveat}")
    if year is None:
        return "unknown", "no publication year and no author death year"
    # an author must have been alive at publication; even with a 95-year life
    # span the work is certainly PD once publication + 95 + 70 has passed
    if year + 165 < as_of:
        return ("public_domain",
                f"published {year}: even if an author had lived 95 more years "
                f"(dying {year + 95}), life+70 expired 1 Jan {year + 166}; "
                f"PD with unknown/any author death")
    if year > last_pd_death:
        # even a posthumous author would have to have died by 1955 to be PD now
        caveat = (" (multi-author: term runs from the last surviving author; "
                  "all deaths unknown)") if n_authors > 1 else ""
        return ("presumed_in_copyright",
                f"published {year}: PD would require the author to have died by "
                f"{last_pd_death}, i.e. before/at publication; author death year not "
                f"found, so protected by default{caveat}")
    if n_authors > 1:
        return ("unknown",
                f"published {year}: {n_authors} authors, death years unknown; "
                f"EU term runs from the last surviving author, cannot be decided")
    return ("presumed_public_domain",
            f"published {year} (<= {last_pd_death}): anonymous/corporate rule "
            f"(publication+70, expired 1 Jan {year + EU_TERM_YEARS + 1}) makes this "
            f"PD unless the named author died after {last_pd_death}; "
            f"author death year not found")


def overall(us: str, eu: str) -> str:
    u, e = us.replace("presumed_", ""), eu.replace("presumed_", "")
    if u == "public_domain" and e == "public_domain":
        exact = us == eu == "public_domain"
        return "public_domain_both" if exact else "public_domain_both (partly presumed)"
    if u == "public_domain":
        return "public_domain_us_only"
    if e == "public_domain":
        return "public_domain_eu_only"
    if u == "in_copyright" and e == "in_copyright":
        exact = us == eu == "in_copyright"
        return "in_copyright_both" if exact else "in_copyright_both (presumed)"
    return "undetermined_mixed"


# --------------------------------------------------------------------------- #
# Several books in one cell
# --------------------------------------------------------------------------- #

STATUS_RANK = ["public_domain", "presumed_public_domain", "in_copyright",
               "presumed_in_copyright", "unknown"]
PER_BOOK_FIELDS = [
    "book_no", "book_source", "book_reference", "parsed_author",
    "parsed_title", "parsed_year", "parsed_year_original",
    "author_death_year", "us_status", "eu_status", "rights_overall",
    "rights_confidence", "us_basis", "eu_basis", "public_domain_note",
    "rights_notes",
]


def _best_status(statuses: Iterable[str]) -> str:
    """Most permissive status in the list (a PD finding beats 'in copyright')."""
    vals = [s for s in statuses if s]
    if not vals:
        return "unknown"
    return min(vals, key=lambda s: STATUS_RANK.index(s)
               if s in STATUS_RANK else len(STATUS_RANK))


def _book_source(parsed: ParsedBook) -> str:
    if parsed.urls:
        return Path(urlparse(parsed.urls[0]).path).name
    return ""


def _book_ref(n: int, parsed: ParsedBook) -> str:
    who = parsed.query_author or parsed.full_author or "?"
    year = parsed.year if parsed.year else "n.d."
    src = _book_source(parsed)
    return f"{n}) {who} {year}" + (f" [{src}]" if src else "")


def _is_pd(status: str) -> bool:
    return status in ("public_domain", "presumed_public_domain")


def _pd_note(us: str, eu: str) -> str:
    """Short jurisdiction note for a book that is public domain somewhere."""
    us_pd, eu_pd = _is_pd(us), _is_pd(eu)
    if not (us_pd or eu_pd):
        return ""
    how = "clear" if "presumed" not in (us + eu) else "presumed"
    if us_pd and eu_pd:
        return f"PD in both USA+EU ({how})"
    if us_pd:
        return f"PD in USA only ({us.replace('_', ' ')})"
    return f"PD in EU only ({eu.replace('_', ' ')})"


def _per_book_record(idx: int, n_books: int, book_no: int,
                     parsed: ParsedBook, row: dict) -> dict:
    rec = {"row_index": idx, "book_no": book_no, "books_in_cell": n_books}
    for f in PER_BOOK_FIELDS[1:]:
        rec[f] = row.get(f, "")
    # these three describe the book itself, not the row
    rec["book_source"] = _book_source(parsed)
    rec["book_reference"] = _book_ref(book_no, parsed)
    rec["public_domain_note"] = _pd_note(row.get("us_status", ""),
                                         row.get("eu_status", ""))
    return rec



def confidence(parsed: ParsedBook, matched: Candidate | None, score_: float,
               death: int | None) -> str:
    """Confidence in the publication year, not in the identity match.

    high   -- the Book-cell year is corroborated (within 1 year) by a strong
              catalogue match
    medium -- the year was written in the Book cell
    low    -- the Book cell has no year; the verdict can only rest on a death
              year (or on a catalogue match that is *not* used as the year)
    none   -- neither a Book-cell year nor a death year
    """
    if parsed.year and parsed.kind in ("citation", "url", "filename"):
        base = "medium"          # explicit year in the source data
    elif death:
        base = "low"             # no publication year in the Book cell
    else:
        return "none"
    if parsed.year and matched is not None and score_ >= 0.75 and matched.year and \
            abs(parsed.year - matched.year) <= 1:
        base = "high"
    return base


# --------------------------------------------------------------------------- #
# One row
# --------------------------------------------------------------------------- #

FIELDS = [
    "book_parse_type", "parsed_author", "parsed_title", "parsed_year",
    "parsed_year_original", "parsed_publisher", "lookup_source",
    "lookup_match", "match_score", "lookup_year", "license",
    "author_death_year", "us_status", "eu_status", "rights_overall",
    "rights_confidence", "us_basis", "eu_basis", "access_evidence",
    "rights_notes", "books_in_cell", "books_summary", "public_domain_books",
]


def short_title(title: str, n: int = 10) -> str:
    """First significant title chunk, for catalogue queries.

    'Dictionnaire fang-français et français-fang, suivi ...' ->
    'Dictionnaire fang-français et français-fang'
    """
    t = clean(title)
    t = re.split(r"\s=\s|\s/\s|\s:\s|[,;:]", t)[0]
    t = " ".join(t.split()[:n])
    return clean(t)


def build_queries(parsed: ParsedBook) -> list[tuple[str, str]]:
    """(label, query) pairs, ordered by expected usefulness."""
    a, t, y = parsed.query_author, short_title(parsed.title), parsed.year
    q = []
    if parsed.kind in ("url", "filename") and a:
        q.append(("author+title-hint", f"{a} {t}".strip()))
    if t and a:
        q.append(("title+author", f"{t} {a}"))
        q.append(("title+author+year", f"{t} {a} {y}" if y else f"{t} {a}"))
    elif t:
        q.append(("title", t))
    elif a and y:
        q.append(("author+year", f"{a} {y}"))
    return q[:2]


def ia_query(text: str) -> str:
    """Make a free-text query safe for archive.org's Lucene syntax."""
    text = re.sub(r'[:()\[\]{}"!^~?\\/]', " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:120]


def _plausible_death(death: int | None, year: int | None, notes: list[str],
                     trace: Trace) -> int | None:
    """Reject death years that cannot belong to the author of this book.

    A person who died years before the book appeared is the wrong person, and
    so is a person who would have had to live well over a century to sign a
    work published long after their plausible lifespan.
    """
    if death and year and death < year - 5:
        trace(f"[warn] death {death} precedes publication {year} by more than "
              f"5 years: identity not credible, death year ignored")
        notes.append(f"authority-file death {death} rejected as implausible for a "
                     f"{year} publication")
        return None
    if death and year and death > year + 95:
        trace(f"[warn] death {death} is more than 95 years after publication "
              f"{year}: namesake, death year ignored")
        notes.append(f"authority-file death {death} rejected as implausible "
                     f"(namesake) for a {year} publication")
        return None
    return death


def _implausible_lifetime(info: dict | None, year: int | None) -> str:
    """Explain why a VIAF/Wikipedia identity cannot be the author, if so.

    An author must have been old enough to write the book; a birth only a few
    years before publication means the namesake is somebody else.
    """
    if not info or not year:
        return ""
    birth = info.get("birth")
    if birth and not (15 <= year - birth <= 100):
        return (f"{info.get('label')!r} was born {birth}, "
                f"{year - birth} year(s) before the {year} publication")
    return ""


def _override_for_book(ov, parsed: ParsedBook, n_books: int) -> int | None:
    """Manual death year for this row, applied only to the book it names.

    ``overrides`` maps row_index -> (death_year, author-as-written).  In a cell
    with several books the override must not be attached to the wrong author,
    so the surname tokens have to overlap when more than one book is present.
    """
    if not ov:
        return None
    if isinstance(ov, (tuple, list)):
        death, author = ov[0], (ov[1] if len(ov) > 1 else "")
    else:
        death, author = ov, ""
    if n_books > 1 and str(author).strip():
        wanted = set(re.findall(r"[a-z]+", norm(str(author))))
        names = [norm(a) for a in parsed.authors] + [norm(parsed.full_author)]
        have = set()
        for name in names:
            have |= set(re.findall(r"[a-z]+", name))
        if not (wanted & have):
            return None
    try:
        return int(death)
    except (TypeError, ValueError):
        return None


def _process_book(idx: int, value: Any, parsed: ParsedBook, providers: Providers,
                  http: Http, trace: Trace, overrides: dict, as_of: int,
                  book_no: int = 1, n_books: int = 1) -> dict:
    """Look up and decide one book (one row may contain several)."""
    trace.rule("-")
    if n_books > 1:
        trace(f"ROW {idx:03d} BOOK {book_no}/{n_books}  |  "
              f"cell: {str(value)[:120]!r}")
    else:
        trace(f"ROW {idx:03d}  |  cell: {str(value)[:120]!r}")
    trace(f"PARSE  kind={parsed.kind}  author={parsed.full_author!r}  "
          f"year={parsed.year}  orig_year={parsed.original_year}")

    trace(f"       title={parsed.title!r}")
    if parsed.publisher:
        trace(f"       publisher={parsed.publisher!r}")
    for n in parsed.notes:
        trace(f"       note: {n}")

    row = {f: "" for f in FIELDS}
    row["book_parse_type"] = parsed.kind
    row["parsed_author"] = parsed.full_author
    row["parsed_title"] = parsed.title
    row["parsed_year"] = parsed.year
    row["parsed_year_original"] = parsed.original_year
    row["parsed_publisher"] = parsed.publisher

    if parsed.kind in ("empty", "bibkey", "junk"):
        us, usb = us_rights(parsed.year, as_of)
        eu, eub = eu_rights(parsed.year, None, len(parsed.authors), as_of)
        row.update(us_status=us, us_basis=usb, eu_status=eu, eu_basis=eub,
                   rights_overall=overall(us, eu), rights_confidence="none",
                   lookup_source="none",
                   rights_notes="; ".join(parsed.notes) or "no bibliographic data")
        trace(f"DECISION  US={us} ({usb})")
        trace(f"          EU={eu} ({eub})")
        trace(f"          => {row['rights_overall']} / confidence none "
              f"[no lookup attempted]")
        return row

    # ---- network lookups -------------------------------------------------
    candidates: list[Candidate] = []
    queries = build_queries(parsed)
    if not queries:
        queries = [("raw", clean(str(value))[:120])]

    death_info = None
    death_query_name = parsed.person_name
    if len(death_query_name.split()) < 2:
        if death_query_name:
            trace(f"[death] skipped: {death_query_name!r} is a bare surname, "
                  f"too ambiguous for a reliable death year")
        death_query_name = ""

    trace("QUERIES")
    for label, q in queries:
        trace(f"  {label:18s} -> {q!r}")
    trace("  (providers are queried with the first query; the rest are kept "
          "for reference)")

    jobs = {}
    with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for label, q in queries[:1]:
            for fn_name in ("google_books", "openlibrary"):
                jobs[pool.submit(getattr(providers, fn_name), q, parsed=parsed)] = fn_name
            jobs[pool.submit(providers.crossref, q)] = "crossref"
            jobs[pool.submit(providers.internet_archive, ia_query(q))] = "internet_archive"
        if parsed.kind == "archive_item":
            ident = re.search(r"details/([A-Za-z0-9._-]+)", parsed.urls[0]).group(1)
            jobs[pool.submit(providers.ia_item, ident)] = "ia_item"
        if death_query_name:
            jobs[pool.submit(providers.person_death, death_query_name,
                             parsed.year)] = "death"

        seen = set()
        for fut in futures.as_completed(jobs):
            name = jobs[fut]
            try:
                result = fut.result()
            except ProviderUnavailable as exc:
                trace(f"[{name}] unavailable: {exc}")
                continue
            except Exception as exc:                      # never kill the run
                trace(f"[{name}] error: {type(exc).__name__}: {exc}")
                continue
            if name == "death":
                reason = _implausible_lifetime(result, parsed.year)
                if reason:
                    trace(f"[death] ignored: {reason}")
                    result = None
                death_info = result
                if result:
                    for src_key in ("viaf", "wikipedia"):
                        src = result.get(src_key)
                        if src:
                            trace(f"[{src_key}] {src['label']} | "
                                  f"b. {src['birth']} d. {src['death']} | "
                                  f"match={src['label_sim']}")
                    trace(f"[death] chosen {result['death']} "
                          f"(source: {result['source']})")
                    if result.get("conflict"):
                        trace(f"[warn] {result['conflict']}")
                else:
                    trace(f"[death] no credible death year for {death_query_name!r} "
                          f"(VIAF/Wikipedia)")
                continue
            for c in result or []:
                key = (c.source, norm(c.title), c.year)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(c)

    trace("MATCHING (score = 0.55*title + 0.30*author + 0.15*year; accept >= 0.55)")
    best, best_score = pick_best(parsed, candidates, trace)

    # Prefer the catalogue's fuller author name for the death lookup: the cell
    # often carries only a surname ('mertens') or initials ('J. Mertens').
    if best and best.authors and best_score >= 0.7:
        name = person_name_from(best.authors[0])
        if name and len(name.split()) >= 2 and norm(name) != norm(death_query_name):
            try:
                extra = providers.person_death(name, parsed.year)
            except Exception:
                extra = None
            reason = _implausible_lifetime(extra, parsed.year)
            if reason:
                trace(f"[death] ignored catalogue-author match: {reason}")
                extra = None
            if extra and extra.get("death"):
                for src_key in ("viaf", "wikipedia"):
                    src = extra.get(src_key)
                    if src:
                        trace(f"[{src_key}] catalogue author {name!r} -> "
                              f"{src['label']} | b. {src['birth']} "
                              f"d. {src['death']} | match={src['label_sim']}")
                if death_info and death_info.get("death") and \
                        death_info["death"] != extra["death"]:
                    trace(f"[death] catalogue author {name!r} gives d. {extra['death']}, "
                          f"replacing {death_query_name!r} -> d. {death_info['death']}")
                death_info = extra

    death = death_info["death"] if death_info else None
    death_source = death_info["source"] if death_info else ""
    death_conflict = death_info.get("conflict") if death_info else ""
    ov = _override_for_book(overrides.get(idx), parsed, n_books)
    if ov:
        death = int(ov)
        death_source = "manual override"
        death_conflict = ""
        trace(f"OVERRIDE author death year = {death} (from the overrides file)")

    # STRICT: the publication year comes from the Book cell only.  A catalogue
    # year is informational (lookup_year) and never feeds the rights engine.
    year = parsed.year
    notes = list(parsed.notes)
    if death_conflict:
        notes.append(death_conflict)

    # sanity-check the death year against the publication year: a person who
    # died years before the book appeared is almost certainly the wrong person
    # (done again after the catalogue-author retry below)
    death = _plausible_death(death, parsed.year, notes, trace)

    if best is not None:
        trace(f"BEST MATCH -> {best.source}: {best.title} | "
              f"{', '.join(best.authors[:3])} | {best.year} | "
              f"{best.publisher} | score {best_score}")
        if parsed.year and best.year and abs(parsed.year - best.year) > 1:
            notes.append(f"catalogue year {best.year} differs from cited {parsed.year}; "
                         f"citation year used")
        if not parsed.year and best.year:
            notes.append(f"no year in the Book cell; catalogue year {best.year} is "
                         f"recorded in lookup_year but not used for the verdict")
        if best.public_domain:
            notes.append(f"{best.source} marks the item public domain")
        if best.access:
            notes.append(f"access={best.access}")
        if best.license:
            notes.append(f"license={best.license}")
        if best.isbn:
            notes.append(f"isbn={best.isbn}"
                         + (" (isbnlib available)" if _has_isbnlib() else ""))
            if USE_ISBNLIB:
                meta = isbnlib_lookup(best.isbn)
                if meta:
                    trace(f"[isbnlib] {best.isbn} -> {meta.get('Title')} | "
                          f"{meta.get('Authors')} | {meta.get('Year')} | "
                          f"{meta.get('Publisher')}")
                    notes.append(f"isbnlib confirms: {meta.get('Title')} "
                                 f"({meta.get('Year')})")
                else:
                    trace(f"[isbnlib] no metadata returned for {best.isbn} "
                          f"(provider rate-limited or unknown ISBN)")
        match_str = (f"{best.title[:80]} | {', '.join(best.authors[:2])} | {best.year or ''}")
        row["lookup_source"] = best.source
        row["lookup_match"] = match_str
        row["match_score"] = best_score
        row["lookup_year"] = best.year or ""
        row["license"] = best.license or (
            "public_domain (source flag)" if best.public_domain else "")
        row["access_evidence"] = "; ".join(
            x for x in [best.access, best.license,
                        "public_domain_flag" if best.public_domain else ""] if x)
    else:
        trace(f"NO ACCEPTABLE MATCH (best score {best_score}); "
              f"rights computed from the citation/file name only")
        row["lookup_source"] = "none"
        row["access_evidence"] = ""

    # (the catalogue-author lookup above already ran when the match was strong)

    death = _plausible_death(death, parsed.year, notes, trace)
    if death and death_source:
        notes.append(f"death year from {death_source}")
    row["author_death_year"] = death
    us, usb = us_rights(year, as_of)
    eu, eub = eu_rights(year, death, max(1, len(parsed.authors)), as_of)

    # a reprint of an older edition: mention when the *original* is already PD
    if parsed.original_year and parsed.original_year != year:
        us_orig, us_orig_basis = us_rights(parsed.original_year, as_of)
        notes.append(f"original edition {parsed.original_year}: US={us_orig} "
                     f"({us_orig_basis}); the reprint date {year} was used above")

    conf = confidence(parsed, best, best_score, death)

    row.update(us_status=us, us_basis=usb, eu_status=eu, eu_basis=eub,
               rights_overall=overall(us, eu), rights_confidence=conf,
               rights_notes="; ".join(notes))
    if notes:
        trace("NOTES  " + "; ".join(notes))
    trace(f"DECISION  US={us}  <- {usb}")
    trace(f"          EU={eu}  <- {eub}")
    trace(f"          overall={row['rights_overall']}  confidence={conf}")
    return row


def process_row(idx: int, value: Any, providers: Providers, http: Http,
                trace: Trace, overrides: dict, as_of: int
                ) -> tuple[dict, list[dict]]:
    """Process every book in a Book cell; returns (row, per-book records).

    A cell with one work keeps the original behaviour.  A cell with several
    links gets one full lookup/verdict per distinct book, a readable per-book
    summary in the output, and a row verdict that says whether *some* of the
    books are already public domain.
    """
    books = parse_cell_all(value)
    if len(books) <= 1:
        parsed = books[0]
        row = _process_book(idx, value, parsed, providers, http, trace,
                            overrides, as_of)
        rec = _per_book_record(idx, 1, 1, parsed, row)
        row["books_in_cell"] = 1
        row["books_summary"] = (
            f"{rec['book_reference']} -> US={row['us_status']}, "
            f"EU={row['eu_status']}")
        row["public_domain_books"] = (
            f"{rec['book_reference']}: {rec['public_domain_note']}"
            if rec["public_domain_note"] else "none")
        return row, [rec]

    n_books = len(books)
    book_rows: list[dict] = []
    recs: list[dict] = []
    for n, parsed in enumerate(books, 1):
        brow = _process_book(idx, value, parsed, providers, http, trace,
                             overrides, as_of, book_no=n, n_books=n_books)
        book_rows.append(brow)
        recs.append(_per_book_record(idx, n_books, n, parsed, brow))

    # ---- aggregate the per-book verdicts --------------------------------
    best_us = _best_status(r["us_status"] for r in book_rows)
    best_eu = _best_status(r["eu_status"] for r in book_rows)
    agg = overall(best_us, best_eu)
    verdicts = {r["rights_overall"] for r in book_rows}
    if len(verdicts) > 1:
        if agg.startswith("public_domain"):
            agg += " [some books]"
        elif "in_copyright" in agg:
            agg += " [some books uncertain]"

    row = dict(book_rows[0])            # descriptive columns = book 1
    row["us_status"], row["eu_status"] = best_us, best_eu
    row["rights_overall"] = agg
    row["books_in_cell"] = n_books
    row["books_summary"] = "; ".join(
        f"{r['book_reference']} -> US={r['us_status']}, EU={r['eu_status']}"
        for r in recs)
    pd_recs = [r for r in recs if r["public_domain_note"]]
    row["public_domain_books"] = "; ".join(
        f"{r['book_reference']}: {r['public_domain_note']}"
        for r in pd_recs) or "none"
    conf_rank = {"high": 0, "medium": 1, "low": 2, "none": 3}
    row["rights_confidence"] = min(
        (r["rights_confidence"] or "none" for r in book_rows),
        key=lambda c: conf_rank.get(c, 9))
    row["rights_notes"] = (
        f"{n_books} books in this cell; row-level parsed_*/lookup_* fields "
        f"describe book 1 ({_book_ref(1, books[0])}); per-book verdicts are in "
        f"books_summary, public_domain_books and "
        f"{PER_BOOK_CSV}; " + str(row.get("rights_notes", "")))

    trace.rule("-")
    trace(f"ROW {idx:03d} AGGREGATE over {n_books} books")
    for r in recs:
        trace(f"  {r['book_reference']:42s} US={r['us_status']:22s} "
              f"EU={r['eu_status']:22s} => {r['public_domain_note'] or 'not PD'}")
    trace(f"  PUBLIC DOMAIN BOOKS: {row['public_domain_books']}")
    trace(f"  overall={agg}  confidence={row['rights_confidence']}")
    return row, recs



def _has_isbnlib() -> bool:
    try:
        import isbnlib  # noqa: F401
        return True
    except ImportError:
        return False


USE_ISBNLIB = False


def isbnlib_lookup(isbn: str) -> dict | None:
    """Optional cross-check of an ISBN discovered by a catalogue match.

    The Book column itself has no ISBNs, so this is never the primary route;
    it only confirms publisher/year for records where one was found.
    """
    if not isbn or not _has_isbnlib():
        return None
    try:
        import isbnlib
        meta = isbnlib.meta(isbn)
    except Exception:
        return None
    return meta or None


def _write_output(work: pd.DataFrame, rows_by_index: dict[int, dict],
                  path: str) -> pd.DataFrame | None:
    """Write whatever has been computed so far (also used for partial saves)."""
    idx = [i for i in work.index if i in rows_by_index]
    if not idx:
        return None
    base = work.loc[idx].reset_index(drop=True)
    extra = pd.DataFrame([rows_by_index[i] for i in idx]).reset_index(drop=True)
    out = pd.concat([base, extra], axis=1)
    out.index = idx                  # keep original row numbers
    for col in ("parsed_year", "parsed_year_original", "lookup_year",
                "author_death_year"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
    out.to_csv(path, index=False)
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--input", default=INPUT_CSV)
    ap.add_argument("--output", default=OUTPUT_CSV)
    ap.add_argument("--per-book-output", default=PER_BOOK_CSV)
    ap.add_argument("--log", default=LOG_FILE)
    ap.add_argument("--cache", default=CACHE_FILE)
    ap.add_argument("--overrides", default=OVERRIDES_FILE)
    ap.add_argument("--as-of-year", type=int, default=AS_OF_YEAR)
    ap.add_argument("--limit", type=int, default=0, help="only the first N rows")
    ap.add_argument("--rows", default="", help="comma separated row indices to process")
    ap.add_argument("--offline", action="store_true", help="use the cache only")
    ap.add_argument("--isbnlib", action="store_true",
                    help="cross-check ISBNs found by the catalogues with isbnlib")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    global USE_ISBNLIB
    USE_ISBNLIB = args.isbnlib

    trace = Trace(Path(args.log), echo=not args.quiet)
    http = Http(Path(args.cache), args.offline, trace)
    providers = Providers(http, trace)

    df = pd.read_csv(args.input)
    work = df.head(args.limit) if args.limit else df
    if args.rows:
        wanted = [int(x) for x in re.split(r"[,\s]+", args.rows.strip()) if x]
        work = df.loc[[i for i in wanted if i in df.index]]

    overrides: dict[int, tuple[int, str]] = {}
    ov_path = Path(args.overrides)
    if ov_path.exists():
        ov_df = pd.read_csv(ov_path)
        for _, r in ov_df.iterrows():
            try:
                if pd.notna(r.get("death_year")):
                    author = r.get("parsed_author")
                    overrides[int(r["row_index"])] = (
                        int(r["death_year"]),
                        "" if pd.isna(author) else str(author))
            except (ValueError, TypeError):
                continue
        trace(f"[overrides] loaded {len(overrides)} manual death year(s) "
              f"from {ov_path}")

    trace.rule()
    trace("SIGTYP DICTIONARY RIGHTS LOOKUP")
    trace(f"run started {datetime.now().isoformat(timespec='seconds')}  |  "
          f"rows {len(work)}  |  as-of year {args.as_of_year}  |  "
          f"offline={args.offline}  |  isbnlib={'yes' if _has_isbnlib() else 'not installed'}")
    trace.rule()
    trace("JURISDICTION RULES")
    trace(f"  USA : PD if published <= {args.as_of_year - 96} (95-year term); "
          f"1931-1977 = publication+95 with renewal/URAA caveat; >=1978 protected.")
    trace(f"  EU  : PD if author died <= {args.as_of_year - 71} (life+70); if the "
          f"author is unknown, publication+70 for anonymous works.")
    trace("        death years come from VIAF, then the English Wikipedia; the")
    trace("        Book-cell year is always the publication year.")
    trace("  Statuses: public_domain | presumed_public_domain | in_copyright |")
    trace("            presumed_in_copyright | unknown.  'presumed' always carries")
    trace("            the reason in its *_basis column.")
    trace("PIPELINE")
    trace("  1 parse Book cell -> author/title/year/publisher")
    trace("     a cell with several links holds several books: each link is")
    trace("     parsed, looked up and judged separately (books_in_cell,")
    trace("     books_summary, public_domain_books, and the per-book CSV)")
    trace("     the publication year is taken STRICTLY from this cell; it is the")
    trace("     only year that feeds the rights rules")
    trace("  2 query Google Books, Open Library, Crossref, Internet Archive")
    trace("     (identity / licence evidence; their year goes to lookup_year only)")
    trace("  3 resolve the first author's death year: VIAF first, Wikipedia second")
    trace("     (the *name* may be completed from a strong catalogue match, but the")
    trace("     death year itself comes only from those two sites)")
    trace("  4 fuzzy-match candidates (title/author/year) and keep the best")
    trace("  5 apply the US and EU rules to the Book-cell year (+ death year)")
    trace("OUTPUT COLUMNS (appended to the input table)")
    trace("  book_parse_type     citation | url | filename | archive_item | bibkey | junk | empty")
    trace("  parsed_*            what step 1 extracted from the Book cell")
    trace("  lookup_source       provider that supplied the accepted match (or 'none')")
    trace("  lookup_match        matched title | authors | year")
    trace("  match_score         0.55*title + 0.30*author + 0.15*year (+0.05 dictionary bonus)")
    trace("  lookup_year         year printed by the catalogue -- REFERENCE ONLY, never used")
    trace("  license             CC/public-domain licence URL reported by the source, if any")
    trace("  author_death_year   first author's death year (VIAF, else Wikipedia) if found")
    trace("  us_status / eu_status   verdicts; us_basis / eu_basis explain them")
    trace("  rights_overall      combined verdict")
    trace("  rights_confidence   high (Book-cell year corroborated) | medium (year from the cell) |")
    trace("                      low (no year in the cell; death year only) | none")
    trace("  access_evidence     provider access/licence flags (not a legal status)")
    trace("  rights_notes        parse notes, year discrepancies, rejected death years")
    trace("  books_in_cell       how many distinct works the Book cell contains")
    trace("  books_summary       one US/EU verdict per book in the cell")
    trace("  public_domain_books which of those books are public domain, and where")
    trace("")
    trace("HOW TO READ THE LOG")
    trace("  one block per row: PARSE -> QUERIES -> MATCHING -> DECISION -> [row time]")
    trace("  a row holding several books gets one full block per book plus a")
    trace("  closing AGGREGATE block naming the public-domain books")
    trace("  everything is written even when a provider fails; failures never abort a row")
    trace.rule()

    rows_by_index: dict[int, dict] = {}
    per_book: list[dict] = []

    def run_one(idx: int, value: Any) -> tuple[list[str], dict, list[dict]]:
        local = RowLog()
        t0_row = time.time()
        recs: list[dict] = []
        try:
            row, recs = process_row(idx, value, providers, http, local, overrides,
                                    args.as_of_year)
        except Exception as exc:                          # last-resort guard
            local(f"ROW {idx:03d} FAILED: {type(exc).__name__}: {exc}")
            row = {f: "" for f in FIELDS}
            row.update(book_parse_type="error", us_status="unknown",
                       eu_status="unknown", rights_overall="unknown",
                       rights_confidence="none",
                       rights_notes=f"lookup failed: {exc}")
        local(f"[row time] {time.time() - t0_row:.1f}s")
        return local.lines, row, recs

    t0 = time.time()
    total = len(work)
    done = 0
    with futures.ThreadPoolExecutor(max_workers=ROW_WORKERS) as pool:
        jobs = {pool.submit(run_one, int(i), v): int(i)
                for i, v in work[BOOK_COLUMN].items()}
        for fut in futures.as_completed(jobs):
            idx = jobs[fut]
            try:
                lines, row, recs = fut.result()
            except Exception as exc:                      # threads should not raise
                lines = [f"ROW {idx:03d} FAILED OUTSIDE HANDLER: {exc}"]
                recs = []
                row = {f: "" for f in FIELDS}
                row.update(book_parse_type="error", us_status="unknown",
                           eu_status="unknown", rights_overall="unknown",
                           rights_confidence="none", rights_notes=str(exc))
            trace.block(lines)
            done += 1
            rows_by_index[idx] = row
            per_book.extend(recs)
            if done % 10 == 0 or done == total:
                trace(f"[progress] {done}/{total} rows, {time.time() - t0:.0f}s, "
                      f"http cache hits={http.stats['hit']} "
                      f"misses={http.stats['miss']} errors={http.stats['error']}")
                http.save()
                _write_output(work, rows_by_index, args.output)

    rows = [rows_by_index[i] for i in work.index if i in rows_by_index]
    out = _write_output(work, rows_by_index, args.output)

    # ---- per-book table (one line per work in a multi-book cell) ---------
    pb_df = pd.DataFrame(per_book)
    if not pb_df.empty:
        pb_df = pb_df.sort_values(["row_index", "book_no"])
        pb_df.to_csv(args.per_book_output, index=False)

    # ---- summary + curation template -------------------------------------
    summary = out.groupby(["rights_overall", "rights_confidence"]).size()
    trace.rule()
    trace("SUMMARY")
    for (status, conf), n in summary.items():
        trace(f"  {n:4d}  {status:45s} confidence={conf}")
    trace("")
    trace("  status counts (EU): " + ", ".join(
        f"{k}={v}" for k, v in out["eu_status"].value_counts().items()))
    trace("  status counts (US): " + ", ".join(
        f"{k}={v}" for k, v in out["us_status"].value_counts().items()))
    trace("  lookup sources    : " + ", ".join(
        f"{k}={v}" for k, v in out["lookup_source"].value_counts().items()))
    multi = out[out["books_in_cell"] > 1]
    trace(f"  books             : {int(out['books_in_cell'].sum())} in "
          f"{len(out)} rows; {len(multi)} row(s) contain more than one book")
    trace(f"  per-book table    : {args.per_book_output} "
          f"({len(pb_df)} rows)")
    any_pd_us = out["us_status"].isin(["public_domain", "presumed_public_domain"])
    any_pd_eu = out["eu_status"].isin(["public_domain", "presumed_public_domain"])
    trace(f"  rows with >=1 PD book: USA={int(any_pd_us.sum())}, "
          f"EU={int(any_pd_eu.sum())}, "
          f"both={int((any_pd_us & any_pd_eu).sum())}")
    if not multi.empty:
        trace("")
        trace("  MULTI-BOOK CELLS -- every book judged separately")
        for i, r in multi.iterrows():
            trace(f"    row {int(i):3d}  {int(r['books_in_cell'])} book(s)  "
                  f"-> {r['rights_overall']}")
            trace(f"        PD : {r['public_domain_books']}")

    unclear = out[(out["rights_confidence"].isin(["none", "low"])) |
                  (out["eu_status"] == "unknown")]
    trace("")
    trace(f"  {len(unclear)} row(s) need human review (see CSV, confidence none/low "
          f"or EU unknown):")
    for _, r in unclear.head(30).iterrows():
        year = r["parsed_year"]
        year_s = "" if pd.isna(year) else str(int(year))
        trace(f"    row {int(r.name):3d}  {str(r['parsed_author'])[:30]:30s} "
              f"{year_s:5s} US={r['us_status']} / EU={r['eu_status']}")

    # only rows where a death year can actually change the EU verdict: works
    # old enough to be PD, or undecided because of multiple authors / no date
    no_death = out["author_death_year"].isna() | \
        (out["author_death_year"].astype(str).str.strip().isin(["", "nan", "None"]))
    need_death = out[no_death &
                     (out["eu_status"].isin(["unknown", "presumed_public_domain"]))]

    templ_rows = []
    for i, r in need_death.iterrows():
        author = "" if pd.isna(r["parsed_author"]) else str(r["parsed_author"])
        if not author.strip() and isinstance(r.get("lookup_match"), str):
            author = r["lookup_match"].split("|")[1].strip() if "|" in r["lookup_match"] else ""
        if not author.strip():
            continue                 # empty/bibkey rows: nothing to curate
        templ_rows.append({
            "row_index": i,
            "parsed_author": author,
            "parsed_year": r["parsed_year"],
            "current_eu_status": r["eu_status"],
            "death_year": overrides.get(i, ("", ""))[0],   # keep earlier curation
            "source_note": "",
        })
    new_templ = pd.DataFrame(templ_rows)
    if ov_path.exists() and not new_templ.empty:
        try:                       # preserve hand-entered death years only
            old = pd.read_csv(ov_path)
            if "row_index" in old.columns:
                if "death_year" in old.columns:
                    old = old[pd.notna(old["death_year"])]
                old = old[~old["row_index"].isin(new_templ["row_index"])]
                new_templ = pd.concat([new_templ, old], ignore_index=True)
        except Exception:
            pass
    if not new_templ.empty:
        new_templ = new_templ.sort_values("row_index")
        new_templ.to_csv(args.overrides, index=False)
        trace("")
        trace(f"  wrote curation template {args.overrides} with {len(new_templ)} "
              f"author(s) whose death year is unknown; fill 'death_year' and re-run "
              f"to sharpen the EU verdicts.")
    else:
        trace("")
        trace(f"  no unresolved authors: {args.overrides} left unchanged")

    trace("")
    trace(f"done in {time.time() - t0:.0f}s  ->  {args.output}, {args.log}, {args.cache}")
    trace.rule()
    trace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
