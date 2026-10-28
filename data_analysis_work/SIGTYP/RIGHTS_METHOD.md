# Rights resolution — method note

How `resolve_rights.py` decides the publication-rights status of every book in
`sigtyp_sub_dicts.csv`, what evidence each column rests on, and what a human
still has to look at.

Scope: **US and EU status, edition by edition**, for the 150 `Book` cells
(192 books) of the SIGTYP sub-dictionary table. `unverified`/`presumed`
verdicts are deliberate: this is a screening pass to decide what can be
reused, not a legal opinion.

## Files

Layout: scripts in `scripts/`, the API key in `api_keys/`, the HTTP cache and
trace in `logs/`, data tables in the repository folder itself.

| file | what it is |
| --- | --- |
| `sigtyp_sub_dicts.csv` | input, never modified by the pipeline |
| `sigtyp_sub_dicts_with_rights_v2.csv` | 150 rows: the input table plus the evidence and verdict columns, rolled up per cell |
| `sigtyp_sub_dicts_rights_per_book_v2.csv` | 192 rows: one line per book, the unit of judgment |
| `rights_summary_v2.md` | readable report (coverage, verdict counts, review queue, PD list) |
| `logs/rights_lookup_v2.log` | full per-book trace: queries, candidates, scores, deaths, decisions |
| `logs/rights_cache_v2.json` | every HTTP response, so re-runs are cheap and mostly offline |
| `backups/<timestamp>/` | a copy of the previous outputs before each run |
| `api_keys/google_books_api_key.txt` | Google Books key (the parent `digitizing/google_api_key.txt` returns 403) |
| `scripts/resolve_rights.py` | the pipeline (run it, it regenerates all `_v2` outputs) |
| `scripts/lookup_rights.py` | shared parsing/lookup/statute code plus the older v1 pipeline |
| `scripts/rank_for_digitization.py` | builds the digitisation worklists (index + priority) from the two `_v2` tables |
| `sigtyp_sub_dicts_digitization_priority_v2.csv` | 150 cells, sorted by priority, with `index` and `priority` columns |
| `sigtyp_sub_dicts_digitization_priority_per_book_v2.csv` | 192 books, same ranking at book level |

## Pipeline

For each book in each cell (`parse_cell_all` splits multi-book cells and keeps
`book_no`):

1. **Parse** the cell. Four shapes occur: a citation, a gramfinder URL, an
   archive.org item, or a bare file name. A year in a URL or file name
   (`krumm_kimatumbi1913.pdf`, `[19??d]`) is kept as `parsed_year_original` but
   only used as a hint.
2. **Identify the edition** in five catalogues concurrently: Google Books
   (keyed), Open Library (work → edition records, `publish_date` not
   `first_publish_year`), Internet Archive item metadata, BnF SRU, and Crossref
   (1970+ only). Google Books receives a short ladder of free-text
   author+language+kind queries; fielded `intitle:`/`inauthor:` operators return
   nothing from this endpoint. Every candidate is scored locally on title,
   author, year and language — API order is never trusted.
3. **Corroborate.** Two independent catalogues agreeing on title+year ⇒
   `identity_level=corroborated`; one ⇒ `single_source`; nothing usable ⇒
   `none`.
4. **Identifiers → HathiTrust.** OCLC/LCCN/ISBN collected above go to the
   HathiTrust Bib API. A `rightsCode` is used **only when the HathiTrust
   record's `publishDates` match the edition year (±2)** — a 1967 reprint says
   nothing about the 1887 edition. `pd`/`pdus` are read as US (and, for `pd`,
   worldwide) evidence; `ic`/`und` are not treated as proof of anything.
5. **Author death.** VIAF → English Wikipedia → Wikidata → BnF creator life
   dates. Multi-author books look up **every** author plus any fuller name the
   matched catalogue records give, because the EU term runs from the last
   surviving author.
6. **Verdict.** Statutory US term (95 years from publication) and EU term
   (life+70) are computed first, then reconciled with service evidence.
7. **Review flag.** `needs_review=yes` only where a lookup cannot settle it.

## Evidence rules

- **Google Books `publicDomain=true` + `viewability=ALL_PAGES`** counts as
  positive public-domain evidence. `publicDomain=false` is ignored: Google marks
  most in-copyright volumes that way, so it proves nothing.
- **HathiTrust `rightsCode`** is only accepted for a date-matched edition (see
  above). `pd` is worldwide; `pdus` is US-only.
- **Internet Archive `possible-copyright-status`** (e.g. `NOT_IN_COPYRIGHT` on
  `vocabulairefran00bareuoft`, row 53) is used as US evidence and recorded, but
  does not override a different edition.
- **Country vs. term.** A service flag is evidence about the digitised copy's
  country. It can add a public-domain finding (e.g. HathiTrust `pd`), never
  create an in-copyright finding that the statutes do not support.
- **Undated but old.** When there is no author death, the EU rule falls back on
  "an author must have been alive at publication": publication+165 years is
  safely PD; publication after 1955 with no death is `presumed_in_copyright`.
- **EU with several authors and no deaths** stays `unknown` — that is row 53.
  A single death that provably runs past 2026 forces `in_copyright`; a death
  that would prove PD needs *all* deaths known, so EU is never flipped to PD on
  partial evidence.

## Editions, reprints and the review flag

`edition_match` compares the cell's year with the best catalogue record:

| value | meaning | effect |
| --- | --- | --- |
| `same` | within ±2 years | verdict on the cell year |
| `n/a` | cell or catalogue has no year | verdict on whatever year exists |
| `earlier_edition` | catalogue year precedes the cell | noted, no flag (the cited edition is later) |
| `reprint` | catalogue record is a modern reissue: POD/facsimile publisher (`POD_PUBLISHER_RE`: Nabu, Kessinger, LINCOM, Forgotten Books, …) or a gap of ≥ 40 years | noted, no flag; verdict stays on the cell year |
| `other` | a real edition mismatch with no reprint explanation | flagged **only if** the US status differs between the two years |

This is why row 124 (LINCOM 2010 record for a 1913 work) and row 144#2
(Bryant 1917) are no longer review rows: the catalogue record is a later
reissue, not a competing edition.

## Death-year sanity checks

`_plausible_death` rejects an authority-file death that cannot belong to the
author of this book:

- death more than 5 years **before** publication ⇒ namesake, ignored;
- death more than 95 years **after** publication ⇒ namesake (the author would
  have been over a century old), ignored.

Rejections are recorded in `rights_notes`. This removed, among others, a
Wikidata "John Whitehead" who died in 2002 for the 1899 Bobangi grammar.

## Hand-checked records

Everything else is automatic. These books got one extra targeted lookup, stored
in `EDITION_VERIFICATIONS` (keyed by `(row_index, book_no)`), and are also
listed in `rights_summary_v2.md`:

| row/book | finding | what it changed |
| --- | --- | --- |
| 3/1 | SIL Cameroon Language & Culture Archives entry 47934 dates the Nomaande lexicon to **2003** (compilers Comité de langue nɔmaándɛ́, Terri R. Scruggs, Carolyn Taylor) | fills an undated cell; US `in_copyright`, EU `presumed_in_copyright` |
| 35/1 | a **1975 second edition** of Horton's *A Dictionary of Luvale* is catalogued by booksellers (first edition 1953); the 1990 Google Books record is a later reprint | clears the false 1975-vs-1990 mismatch |
| 53/1 | the Internet Archive scan of Bareau/Reding 1912 reports `NOT_IN_COPYRIGHT` | confirms US PD; EU stays `unknown` (compilers' deaths unrecorded) |
| 142/5 | Google Books catalogues the claimed **1956** *Xhosa Phrase Book* (rev. B. B. Mdledle); the Open Library 1906 record is an earlier edition | clears the false 1956-vs-1906 mismatch |

Row 135#1 (Tobias/Turvey *English-Kwanyama Dictionary*) needed no manual entry:
the full name "George Wolfe Robert Tobias" from the BnF record resolves on
Wikidata to a 1974 death, which makes the EU status `in_copyright` instead of
`unknown`.

## Determinism

Lookups run in a thread pool, so the candidate list used to arrive in
completion order and score ties could pick a different edition on different
runs (that is what made the review count flap between 4 and 6). `pick_edition`
now breaks every tie on stable record fields (score, Google Books preference,
source, lower-cased title, year, URL), so consecutive runs over the same cache
produce byte-identical per-book output. Verified by running the script twice
and diffing.

## What a human still has to do

Four books are flagged (`needs_review=yes` in the per-book CSV):

| row | why | what would settle it |
| --- | --- | --- |
| 53 | Bareau/Reding 1912: US PD confirmed, EU `unknown` because both compilers' death years are unrecorded | archival research on the two Belgian colonial officials |
| 62 | empty `Book` cell for Mituku; only a comment mentioning Stappers 1973 | identify the actual item |
| 88 | `[19??d]. 19xx. Deutsch-kiHehe Wörterbuch.` — no year in the cell and no dated catalogue record | a library record for this title |
| 108 | `[Anonymous]. 19–. Éléments de dictionnaire français-kifipa.` — no year, anonymous | a library record for this title |

Rows 88 and 108 are undated *in the corpus itself*, not a lookup failure: the
pipeline finds the titles but nothing dates them. Left as `unknown` on purpose.

## Re-running

```bash
python3 scripts/resolve_rights.py                 # full run (cache-backed, ~10 s warm)
python3 scripts/resolve_rights.py --limit 20      # first 20 cells
python3 scripts/resolve_rights.py --rows 8,9,10   # selected cells
python3 scripts/resolve_rights.py --offline       # no network, cache only
python3 scripts/rank_for_digitization.py          # rebuild the priority worklists
```

Paths are resolved from the script location, so these work from any directory.
Every run copies the existing `*_v2` outputs into `backups/<timestamp>/`
first; `--no-backup` skips that. The Google Books key is read from
`api_keys/google_books_api_key.txt` (the parent `digitizing/google_api_key.txt`
returns 403). The run budget is 700 Google Books requests; the current run uses
570 of them.

## Digitisation priorities

`rank_for_digitization.py` re-reads the two `_v2` tables and writes sorted
worklists with an `index` (1 = first to submit) and a `priority` column. The
pipeline's own files are left untouched, because a re-run would overwrite them.

| priority | meaning | cells / books |
| --- | --- | --- |
| 1 | public domain in both USA+EU, not presumed | 9 / 25 |
| 2 | public domain in both, one region presumed | 12 / 16 |
| 3 | public domain in one region only (EU or US) | 9 / 14 |
| 4 | mixed cell: some books PD in both regions | 11 / — |
| 5 | mixed cell: some books PD in one region | 3 / — |
| 6 | undetermined (conflicting evidence) | 2 / 2 |
| 7 | unknown (no usable data) | 1 / 1 |
| 8 | in copyright in both, not presumed | 9 / 15 |
| 9 | in copyright in both, presumed | 90 / 119 |
| 10 | mixed cell: in copyright in both | 4 / — |

Inside a priority, rows are ordered by confidence (high → none), then
verification (verified → unresolved), then original row order. For a
digitisation queue use the per-book file: it splits multi-book cells, so every
row is one book (with its PDF file name where one is known).

## Known limits

- The gramfinder host (`130.60.24.118`) is not reachable, so URL cells are
  resolved from file-name hints plus catalogues; no PDF is fetched.
- Google Books free-text ranking is the workhorse and it is noisy: title-case
  variants and duplicate volumes are common, which is why identity is judged on
  agreement across catalogues, not on a single hit.
- WorldCat is rate-limited (429) and is not used; Crossref is useless for
  pre-1970 books.
- HathiTrust coverage depends on identifiers (34 of 192 books); Open Library
  often has the work but not every edition.
- EU status for anonymous/corporate works and for multi-author works with
  missing deaths is a presumption, not a fact; the basis string in
  `eu_basis`/`rights_notes` says which rule was applied.
