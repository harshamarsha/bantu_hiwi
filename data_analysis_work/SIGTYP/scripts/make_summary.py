#!/usr/bin/env python3
"""Generate rights_summary.md from the rights output tables."""
import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

OUT = str(ROOT / "sigtyp_sub_dicts_with_rights.csv")
PB = str(ROOT / "sigtyp_sub_dicts_rights_per_book.csv")
INP = str(ROOT / "sigtyp_sub_dicts.csv")
LOG = str(ROOT / "rights_lookup.log")
SUMMARY = ROOT / "rights_summary.md"

out = pd.read_csv(OUT)
pb = pd.read_csv(PB)
inp = pd.read_csv(INP)

L = []
a = L.append
a("# Dictionary rights summary")
a("")
a(f"Generated **{datetime.date.today().isoformat()}** from `{INP}` "
  f"({len(inp)} rows) using `lookup_rights.py`.")
a("")
a("## Files")
a("")
a("| File | Content |")
a("|---|---|")
a(f"| `{OUT}` | {len(out)} rows x {len(out.columns)} cols "
  f"= the {len(inp.columns)} input columns unchanged + "
  f"{len(out.columns) - len(inp.columns)} rights columns |")
a(f"| `{PB}` | {len(pb)} rows -- one line per book, each with its own verdict |")
a(f"| `{LOG}` | full per-row trace, ending with a MULTI-BOOK CELLS section |")
a("| `backups/` | timestamped copies of earlier outputs |")
a("")

n_books = int(out["books_in_cell"].sum())
n_multi = int((out["books_in_cell"] > 1).sum())
any_pd = out["public_domain_books"].ne("none")
a("## Totals")
a("")
a(f"- Rows: **{len(out)}**")
a(f"- Books judged: **{n_books}**")
a(f"- Cells holding more than one book: **{n_multi}** "
  f"(largest: {int(out['books_in_cell'].max())})")
a(f"- Rows with at least one public-domain book: **{int(any_pd.sum())}**")
a("")

a("## Verdict distribution (`rights_overall`)")
a("")
a("| Verdict | Rows |")
a("|---|---:|")
for k, v in out["rights_overall"].value_counts().items():
    a(f"| {k} | {v} |")
a("")
a("Jurisdiction counts:")
a("")
a("- EU: " + ", ".join(f"{k}={v}" for k, v in out["eu_status"].value_counts().items()))
a("- US: " + ", ".join(f"{k}={v}" for k, v in out["us_status"].value_counts().items()))
a("")
a("`presumed_*` means the death year of the author could not be established "
  "from VIAF/Wikipedia, so the verdict rests on the publication year alone.")
a("")

a("## Multi-book cells containing at least one public-domain book")
a("")
a("Each line names the book, the verdict for that book and the jurisdiction.")
a("")
m = out[(out["books_in_cell"] > 1) & out["public_domain_books"].ne("none")]
for i, r in m.iterrows():
    a(f"**row {i}** ({int(r['books_in_cell'])} books) -- {r['rights_overall']}")
    a("")
    for part in str(r["public_domain_books"]).split("; "):
        a(f"- {part}")
    a("")

no_pd = out[(out["books_in_cell"] > 1) & out["public_domain_books"].eq("none")]
a("## Multi-book cells with no public-domain book")
a("")
a("Rows " + ", ".join(str(i) for i in no_pd.index) +
  " hold several books but none of them is public domain in the USA or the EU.")
a("")

a("## How to regenerate")
a("")
a("```sh")
a("python3 scripts/lookup_rights.py --quiet   # rewrites the CSVs and the log")
a("python3 scripts/make_summary.py            # rewrites this file")
a("```")
a("")
a("Use `--offline` to reuse the HTTP cache without touching the network, "
  "`--rows 49,96,157` to recheck selected rows, or `--as-of-year 2027` to "
  "shift the US cut-off year.")
a("")

a("## Caveats")
a("")
a("- US verdicts use the as-of-2026 cut-off: published <= 1930 is public domain; "
  "1931-1977 is presumed in copyright (renewal/URAA not checked); >= 1978 is in copyright.")
a("- EU verdicts use life + 70: public domain if the author died <= 1955; without a "
  "death year a work published <= 1955 is only *presumed* public domain.")
a("- Death years come from VIAF first, then English Wikipedia; manual values in "
  "`author_deaths_overrides.csv` win.")
a("- The publication year is taken strictly from the `Book` cell; catalogue years are "
  "recorded in `lookup_year` for reference only.")
a("- `gramfinder` PDF links are unreachable from here, so those rows rely on the "
  "filename (author, language hint, year).")
a("")

with open(SUMMARY, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
print(f"wrote {SUMMARY}", len(L), "lines")
