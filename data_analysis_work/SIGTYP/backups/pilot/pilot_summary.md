# Rights pilot — 20-book coverage report

Sample: **20 books** (forced known cases + stratified by year band + multi-book fill). This is a measurement run; it does not produce legal verdicts.

## Coverage

| Measure | Result |
| --- | --- |
| Books sampled | 20 |
| Hints from prose | 8 |
| Hints from filename/URL | 12 |
| Exact match | 4 |
| Probable match | 6 |
| Lead with year conflict | 5 |
| Weak / none | 5 |
| **HathiTrust item, confirmed edition** | **3** |
| HathiTrust item, lead-only edition | 5 |
| Internet Archive item found | 11 |
| Title-page year read with one candidate year | 3 |
| Title page agrees with catalogue/hint | 3 |
| Title page **disagrees** (edition mismatch) | 0 |
| Title page ambiguous (several years) | 2 |
| Title page inaccessible (IA restriction) | 3 |
| Google Books API answered | 0 |
| Still needs title-page check | 17 |

## Evidence levels

| Level | Books | Meaning |
| --- | --- | --- |
| `no_item_record` | 6 | edition matched, no HT item |
| `identity_unresolved` | 5 | no edition match |
| `edition_unconfirmed` | 5 | lead match, year conflict |
| `ht_pd_all` | 2 | HathiTrust pd — world public domain |
| `ht_edition_mismatch` | 1 | HathiTrust holds a different edition |
| `ht_in_copyright` | 1 | HathiTrust ic |

## Identity basis

| Basis | Books |
| --- | --- |
| title+author (or author+year+token) | 6 |
| — | 5 |
| author+language-token, year conflict (lead) | 5 |
| title+author+year | 4 |

## Per-book detail

| row | src | identity | matched edition | year | TP year | HT codes | items | evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 6 | prose | exact | A grammar of the Bakĕle language, with  | 1854 | 1854 | pd | 6 | `ht_pd_all` |
| 8 | filename | probable | Dictionary and grammar of the Kongo lang | 1887 | ambiguous | ic | 10 | `ht_edition_mismatch` |
| 1 | filename | probable | Dictionnaire duala-français, suivi d'un  | 1972 | ambiguous | ic | 2 | `ht_in_copyright` |
| 0 | prose | probable | English - Lekongho Dictionary | 2019 | no-scan | — | 0 | `no_item_record` |
| 3 | prose | none | Lexique Nomaándé - Français |  | no-scan | — | 0 | `identity_unresolved` |
| 133 | filename | probable | Vocabulary of the Umbundu language | 1885 | 1885 | pd | 1 | `ht_pd_all` |
| 43 | filename | lead_conflict | A Vocabulary Of The Kiteke | 2018 | no-scan | pd | 1 | `edition_unconfirmed` |
| 23 | prose | none | Kleines Ruhaya-Deutsches Wörterbuch | 1914 | no-scan | — | 0 | `identity_unresolved` |
| 144 | filename | lead_conflict | The Zulu people | 2003 | HTTP 404 | ic | 6 | `edition_unconfirmed` |
| 95 | filename | probable | Grammaire kirundi | 1908 | 1908 | — | 0 | `no_item_record` |
| 55 | filename | none | lingombe | 1937 | no-scan | — | 0 | `identity_unresolved` |
| 20 | prose | exact | A Lunyoro-Lunyankole-English and English | 1952 | no-scan | — | 0 | `no_item_record` |
| 142 | filename | lead_conflict | Xosa phrase book (formerly known as the  | 1906 | no-scan | pdus | 1 | `edition_unconfirmed` |
| 40 | filename | probable | Dictionnaire ndumu-mbede-français et fra | 1969 | no-scan | — | 0 | `no_item_record` |
| 142 | filename | lead_conflict | A Xhosa grammar | 1948 | HTTP 403 forbidden | — | 0 | `edition_unconfirmed` |
| 121 | prose | none | Chichewa/Chinyanja - English dictionary: | 2004 | no-scan | — | 0 | `identity_unresolved` |
| 25 | prose | exact | A Jita Vocabulary | 2005 | HTTP 401 restricted | — | 0 | `no_item_record` |
| 134 | filename | lead_conflict | Oshindonga workbook | 1992 | no-scan | ic | 3 | `edition_unconfirmed` |
| 108 | prose | none | Éléments de dictionnaire français-kifipa |  | no-scan | — | 0 | `identity_unresolved` |
| 37 | filename | exact | Le nom en bafia; étude du syntagme nomi | 1973 | HTTP 403 forbidden | — | 0 | `no_item_record` |

## What this means for the full run

- Item-level evidence was reached for **3/20** books with a confirmed edition; a further **5** reached a HathiTrust record only through a `lead` match and are not counted.
- **17/20** need manual identity or a different source.
- **The edition check worked**: one HathiTrust match was for a 1967 reprint while the target was the 1887 edition, so the `ic` code was rejected rather than copied. Another filename said 1886 while its title page said 1888.
- Title-page OCR gave an unambiguous year for 3/20; 2/20 were ambiguous and 3/20 were blocked by Internet Archive restrictions.
- Google Books `publicDomain` was not measurable: keyless calls return HTTP 429 with quota 0. Set `GOOGLE_BOOKS_API_KEY` to enable it.
- `lead` matches rest on author + year + language token, not on a confirmed title. They still require a title-page check.
- The gramfinder PDF host (`130.60.24.118`) returns HTTP 401, so URL-only cells cannot be verified from the item itself.
- The sample deliberately includes hard cases and is 60% filename/URL cells (population: 57%), so treat the extrapolation as an upper-bound estimate.
- Linear extrapolation to 192 books: roughly **98–163** books needing manual resolution, plus a title-page check on the rest.