# Rights pilot

A measurement run — **not** a verdict engine. It answers one question: for a
reproducible sample of `Book` cells, how far does an assumption-free evidence
chain actually get?

## What it does

For each sampled book:

1. Takes author / title / year from the cell **as hints only**. A year read
   from a filename is flagged `hint_source=filename` and is never used as a
   publication fact.
2. Finds candidate editions in Open Library and the Internet Archive.
3. Rejects work-level matches by resolving the **edition** record and using
   its own `publish_date`, not `first_publish_year`.
4. Looks the edition's OCLC / LCCN / ISBN up in the HathiTrust Bibliographic
   API and records `rightsCode` (`pd`, `pdus`, `ic`, `und`).
5. Checks that the HathiTrust record's `publishDates` belong to the **same
   edition**. A rights code attached to a different printing is rejected.
6. Reads the OCR of the item's first pages (`_djvu.txt`) and extracts years,
   discarding library stamps, barcodes and digitisation boilerplate. The hint
   is never used to choose a year.
7. Records Internet Archive rights fields as corroboration only.
8. Emits an evidence level. It never emits `presumed_*` and never turns a
   catalogue flag into a verdict.

## Outputs

| File | Contents |
| --- | --- |
| `pilot_results.csv` | one row per sampled book, every field used for the decision |
| `pilot_summary.md` | coverage report and per-book table |
| `pilot.log` | full trace: queries, candidates, matches, edition checks |
| `pilot_cache.json` | HTTP cache; re-runs are fast and offline-capable |

## Rerun

```bash
python3 pilot/pilot_rights.py
```

Optional: `export GOOGLE_BOOKS_API_KEY=...` to enable the Google Books
`publicDomain` check. Without a key the API returns HTTP 429 with quota 0.

## Sample

20 books, fixed seed (`random.seed(42)`):

- 5 forced cases: Preston 1854 (known `pd`), Bentley 1887 (URL-only),
  Helmlinger 1972 (URL-only), Mehpah 2019, one `n.d.` cell;
- 15 filled by year band, plus multi-book cells.

Population context: 150 rows / 192 books; 57% of cells are URL cells. The
sample is 60% URL cells, so it leans hard.

## What the pilot established

- HathiTrust item-level evidence is available for only a small share of these
  Africanist dictionaries.
- The edition check is essential: one `ic` code belonged to a 1967 reprint of
  an 1887 book, and one filename year (1886) was wrong by two years.
- Title-page OCR from Internet Archive is the strongest fact source, but many
  items are lending-restricted (HTTP 401) or produce several candidate years.
- The `130.60.24.118` gramfinder PDF host returns HTTP 401, so URL-only cells
  cannot be verified from the linked file.
- Google Books `publicDomain` needs a key; keyless calls fail with quota 0.

## What it does not do

- It does not decide legal status, apply the US/EU term rules, or produce a
  final `verified` / `unverified` field.
- It does not read title pages manually. Every `lead`, `ambiguous` and
  `no_item_record` case still needs a human or a better source.
