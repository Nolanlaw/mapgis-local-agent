# MapGIS Agent local patch

Base: [leecugb/mapgis2shp](https://github.com/leecugb/mapgis2shp), commit
`2acf993e81a1d8e1984f110aff6725672ac09149`, version 2.6.0, Apache-2.0.
The upstream license, SPDX markers and author declarations remain attached.
The base distribution version is retained; this document identifies the local change.

Modified on 2026-10-31: `src/pymapgis/writer.py`, `_PolygonArcDecomposer.build`.
The original writer file SHA256 is
`7f283e74aa87683350de3c95e1ea3c085b6d61b25a2f3c906a17110f39d92da2`.

A segment previously held one region number on each side. A second region with
the same boundary direction overwrote the first number, losing identical regions
and producing inconsistent ownership for some partial overlaps. The local patch
allocates another coincident segment slot when a side already belongs to a
different region. Opposite-side neighbours continue to share their original arc.
The chain retains its actual segment identity when reading left/right owners.
Geometry is not snapped, repaired, dissolved or de-duplicated.

The application's region-walk encoder now indexes by owning region as well as
segment coordinates. That adapter remains in `mapgis_agent.creation`; it no
longer mistakes legal coincident arcs from different regions for ambiguous walks.

`tests/test_coincident_regions.py` checks three identical/reversed rings, same-edge
overlap, duplicate regions next to a shared neighbour, and duplicate holes with an
island. Local and nonunit projected scales, separate attributes and physical IDs,
normal BAS60 save/reopen, per-region delete and native subset are exercised.
These checks do not establish support for every proprietary WP auxiliary table.

Setup installs this local source with `--force-reinstall`, so an already installed
PyPI distribution of the same base version cannot silently replace the patch.
Release source/runtime manifests provide the final patched-file fingerprints.

Also modified on2026-10-31: `_encode_line_aux` and its new-file call in
`Writer._sections`. The old code scaled WL coordinates and its common header
by1000/map_scale, but left the head7 record0/per-line bounding boxes in ground
units. At map_scale50000 the native BAS60 range and layer culling were50 times
the actual stored coordinates. Apply the same explicit factor to those boxes.
The optional helper argument defaults to1 for call compatibility; existing raw
section passthroughs and original input files are unchanged.

`tests/test_created_scaled_bounds.py` reproduces the mismatch and checks
WT/WL/WP ranges at1000/50000 through raw sections, actual native open,
normal save and fresh native reopen. The standard sheet workflow additionally
checks mixed WT/WL assembly and real native output. Reader-only geometry
roundtrips had not detected the bad WL drawing ranges.
