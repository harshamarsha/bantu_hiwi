# Dictionary rights summary

Generated **2026-10-25** from `sigtyp_sub_dicts.csv` (168 rows) using `lookup_rights.py`.

## Files

| File | Content |
|---|---|
| `sigtyp_sub_dicts_with_rights.csv` | 168 rows x 31 cols = the 8 input columns unchanged + 23 rights columns |
| `sigtyp_sub_dicts_rights_per_book.csv` | 210 rows -- one line per book, each with its own verdict |
| `rights_lookup.log` | full per-row trace, ending with a MULTI-BOOK CELLS section |
| `backups/` | timestamped copies of earlier outputs |

## Totals

- Rows: **168**
- Books judged: **210**
- Cells holding more than one book: **23** (largest: 7)
- Rows with at least one public-domain book: **40**

## Verdict distribution (`rights_overall`)

| Verdict | Rows |
|---|---:|
| in_copyright_both (presumed) | 87 |
| undetermined_mixed | 35 |
| public_domain_both (partly presumed) | 15 |
| public_domain_both [some books] | 9 |
| public_domain_both | 5 |
| public_domain_eu_only | 5 |
| in_copyright_both | 3 |
| public_domain_us_only | 2 |
| in_copyright_both [some books uncertain] | 2 |
| public_domain_eu_only [some books] | 2 |
| public_domain_us_only [some books] | 2 |
| in_copyright_both (presumed) [some books uncertain] | 1 |

Jurisdiction counts:

- EU: presumed_in_copyright=79, unknown=35, presumed_public_domain=22, in_copyright=18, public_domain=14
- US: in_copyright=71, unknown=34, public_domain=33, presumed_in_copyright=30

`presumed_*` means the death year of the author could not be established from VIAF/Wikipedia, so the verdict rests on the publication year alone.

## Multi-book cells containing at least one public-domain book

Each line names the book, the verdict for that book and the jurisdiction.

**row 49** (2 books) -- public_domain_both (partly presumed)

- 1) sims 1886 [sims_kibangi1886.pdf]: PD in both USA+EU (presumed)
- 2) whitehead 1899 [whitehead_bobangi1899.pdf]: PD in both USA+EU (presumed)

**row 77** (3 books) -- public_domain_both [some books]

- 2) krapf 1887 [krapf_nika-english-dictionary1887.pdf]: PD in both USA+EU (presumed)
- 3) taylor 1891 [taylor_giryama1891_o.pdf]: PD in both USA+EU (clear)

**row 83** (2 books) -- public_domain_both [some books]

- 2) woodward 1882 [woodward_boondei1882_o.pdf]: PD in both USA+EU (clear)

**row 96** (4 books) -- public_domain_both [some books]

- 1) alves 1951 [alves_bundo1951.pdf]: PD in EU only (presumed public domain)
- 3) decannecattim 1804 [decannecattim_bunda1804.pdf]: PD in both USA+EU (clear)

**row 101** (2 books) -- public_domain_both [some books]

- 1) menard 1909 [menard_dictionnaire-kirundi1909_o.pdf]: PD in both USA+EU (clear)

**row 104** (2 books) -- public_domain_eu_only [some books]

- 2) appleby 1943 [appleby_luluhya-dictionary1943.pdf]: PD in EU only (presumed public domain)

**row 105** (4 books) -- public_domain_eu_only [some books]

- 2) jalla 1936 [jalla_lozi1936.pdf]: PD in EU only (presumed public domain)

**row 120** (2 books) -- public_domain_both [some books]

- 2) robertson 1904 [robertson_bemba1904.pdf]: PD in both USA+EU (clear)

**row 145** (2 books) -- public_domain_us_only [some books]

- 2) sanders fay 1885 [sanders-fay_umbundu1885.pdf]: PD in USA only (public domain)

**row 149** (5 books) -- public_domain_both [some books]

- 1) barnes 1932 [barnes_mashonaland1932.pdf]: PD in EU only (public domain)
- 2) biehler 1913 [biehler_chiswina1913.pdf]: PD in both USA+EU (clear)
- 3) biehler 1927 [biehler_chiswina1927.pdf]: PD in both USA+EU (clear)
- 4) elliott 1897 [elliott_tebele-shuna1897.pdf]: PD in both USA+EU (presumed)
- 5) louw 1915 [louw_chikaranga1915.pdf]: PD in both USA+EU (presumed)

**row 157** (6 books) -- public_domain_both [some books]

- 1) bachmann 1890 [bachmann_pondo1890.pdf]: PD in both USA+EU (presumed)
- 3) kropf 1915 [kropf_kaffir1915.pdf]: PD in both USA+EU (clear)

**row 160** (7 books) -- public_domain_both [some books]

- 1) bryant 1905 [bryant_zulu1905.pdf]: PD in both USA+EU (clear)
- 2) bryant 1917 [bryant_zulu1917.pdf]: PD in both USA+EU (clear)
- 3) colenso 1884 [colenso_zulu-english-dictionary1884.pdf]: PD in both USA+EU (clear)
- 6) roberts 1951 [roberts_zulu1951.pdf]: PD in EU only (presumed public domain)

**row 162** (3 books) -- public_domain_us_only [some books]

- 1) elliott 1912 [elliott_sindebele1912.pdf]: PD in USA only (public domain)

**row 165** (2 books) -- public_domain_both [some books]

- 1) junod 1896 [junod_ronga-conversation1896.pdf]: PD in both USA+EU (clear)
- 2) dovalle 1906 [dovalle_shironga1906.pdf]: PD in both USA+EU (presumed)

## Multi-book cells with no public-domain book

Rows 68, 79, 85, 126, 140, 147, 156, 159, 163 hold several books but none of them is public domain in the USA or the EU.

## How to regenerate

```sh
python3 lookup_rights.py --quiet        # rewrites the CSVs and the log
python3 make_summary.py                 # rewrites this file
```

Use `--offline` to reuse the HTTP cache without touching the network, `--rows 49,96,157` to recheck selected rows, or `--as-of-year 2027` to shift the US cut-off year.

## Caveats

- US verdicts use the as-of-2026 cut-off: published <= 1930 is public domain; 1931-1977 is presumed in copyright (renewal/URAA not checked); >= 1978 is in copyright.
- EU verdicts use life + 70: public domain if the author died <= 1955; without a death year a work published <= 1955 is only *presumed* public domain.
- Death years come from VIAF first, then English Wikipedia; manual values in `author_deaths_overrides.csv` win.
- The publication year is taken strictly from the `Book` cell; catalogue years are recorded in `lookup_year` for reference only.
- `gramfinder` PDF links are unreachable from here, so those rows rely on the filename (author, language hint, year).
