#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank_for_digitization.py
========================

Turn the rights tables into a digitisation worklist: one ``index`` column and
one ``priority`` rating, sorted so the books that are safest to digitise come
first.

Priority (1 = submit first):

  1  public domain in both USA+EU, verdict not presumed
  2  public domain in both, one region presumed
  3  public domain in one region only (EU or US)
  4  mixed cell: some books public domain in both regions
  5  mixed cell: some books public domain in one region
  6  undetermined (conflicting evidence)
  7  unknown (no usable data)
  8  in copyright in both regions, verdict not presumed
  9  in copyright in both regions, presumed
 10  mixed cell: in copyright in both regions

Inside a priority the order is confidence (high > medium > low > none), then
verification (verified > single_source > rule_only > unresolved), then the
original row order, so the sort is reproducible.

Outputs (new files; the pipeline's own CSVs are never modified):

    sigtyp_sub_dicts_digitization_priority_v2.csv           150 cells
    sigtyp_sub_dicts_digitization_priority_per_book_v2.csv  192 books

Usage: python3 scripts/rank_for_digitization.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent          # .../SIGTYP/scripts
ROOT = HERE.parent                              # .../SIGTYP
CELL_IN = ROOT / "sigtyp_sub_dicts_with_rights_v2.csv"
BOOK_IN = ROOT / "sigtyp_sub_dicts_rights_per_book_v2.csv"
CELL_OUT = ROOT / "sigtyp_sub_dicts_digitization_priority_v2.csv"
BOOK_OUT = ROOT / "sigtyp_sub_dicts_digitization_priority_per_book_v2.csv"

# rights_overall -> (priority, label)
PRIORITY = {
    "public_domain_both": (1, "PD in both regions"),
    "public_domain_both (partly presumed)": (2, "PD in both regions (presumed)"),
    "public_domain_eu_only": (3, "PD in EU only"),
    "public_domain_us_only": (3, "PD in US only"),
    "public_domain_both [some books]": (4, "Mixed cell: some books PD in both"),
    "public_domain_eu_only [some books]": (5, "Mixed cell: some books PD in EU"),
    "public_domain_us_only [some books]": (5, "Mixed cell: some books PD in US"),
    "undetermined_mixed": (6, "Undetermined"),
    "unknown": (7, "Unknown"),
    "in_copyright_both": (8, "In copyright in both regions"),
    "in_copyright_both (presumed)": (9, "In copyright in both (presumed)"),
    "in_copyright_both [some books]": (10, "Mixed cell: in copyright in both"),
}

CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2, "none": 3, "": 4}
VERIFICATION_RANK = {
    "verified": 0, "single_source": 1, "rule_only": 2, "unresolved": 3, "": 4,
}


def rank(df: pd.DataFrame, row_key: str) -> pd.DataFrame:
    """Attach index/priority and sort the rows into digitisation order."""
    unknown = sorted(set(df["rights_overall"]) - set(PRIORITY))
    if unknown:
        raise SystemExit(f"unmapped rights_overall values: {unknown}")

    df = df.copy()
    df["priority"] = df["rights_overall"].map(lambda v: PRIORITY[v][0])
    df["priority_label"] = df["rights_overall"].map(lambda v: PRIORITY[v][1])
    df["_conf"] = df["rights_confidence"].map(lambda v: CONFIDENCE_RANK.get(v, 4))
    df["_ver"] = df["verification"].map(lambda v: VERIFICATION_RANK.get(v, 4))
    df["_orig"] = range(len(df))

    df = df.sort_values(
        ["priority", "_conf", "_ver", "_orig"], kind="mergesort"
    ).reset_index(drop=True)
    df.insert(0, "index", range(1, len(df) + 1))
    df = df.drop(columns=["_conf", "_ver"])

    # index, priority, label, the original row number, then the source columns
    cols = ["index", "priority", "priority_label", "_orig"] + [
        c for c in df.columns if c not in ("index", "priority", "priority_label", "_orig")
    ]
    df = df[cols].rename(columns={"_orig": row_key})
    return df


def main() -> None:
    cells = pd.read_csv(CELL_IN, dtype=str, keep_default_na=False)
    books = pd.read_csv(BOOK_IN, dtype=str, keep_default_na=False)

    # carry the language identifiers down to the per-book table
    meta = cells.reset_index().set_index("index")
    books = books.copy()
    books["Language"] = books["row_index"].astype(int).map(meta["Language"])
    books["ISO"] = books["row_index"].astype(int).map(meta["ISO"])

    cell_ranked = rank(cells, "original_row_index")
    book_ranked = rank(books, "original_index")
    # keep the language right after the book identifier
    order = list(book_ranked.columns)
    for col in ("Language", "ISO"):
        order.remove(col)
    pos = order.index("book_no") + 1
    order[pos:pos] = ["Language", "ISO"]
    book_ranked = book_ranked[order]

    cell_ranked.to_csv(CELL_OUT, index=False)
    book_ranked.to_csv(BOOK_OUT, index=False)

    print(f"wrote {CELL_OUT.name}: {len(cell_ranked)} cells")
    print(f"wrote {BOOK_OUT.name}: {len(book_ranked)} books")
    counts = cell_ranked.groupby(["priority", "priority_label"]).size()
    for (prio, label), n in counts.items():
        print(f"  priority {prio:2d}  {label:42s} {n}")


if __name__ == "__main__":
    main()
