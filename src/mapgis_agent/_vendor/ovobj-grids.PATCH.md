# Adapted map-sheet kernel

Source: https://github.com/good9527/ovobj-converter/blob/ef5a338f7c45d22c48c841bff8ae059cf75a1cd9/pyovobj/core/grids.py

Commit: ef5a338f7c45d22c48c841bff8ae059cf75a1cd9. Original Git blob57ae9495173494842e28b0d7c12b44ecfa601c9f, SHA25612dde2d05e1583a02be7f5bd65ae3d7091c55f51ec7c0f7941548cba0800d9cb. MIT copyright2026 good9527, retained in ovobj-grids.LICENSE.

This copy retains scale/configuration/normalization and indexing structure, without the unrelated converter, geometry enumeration, GeoDataFrame mutator or package entrypoints. No added dependencies. Fixes:1:2000 is576x576 (25arcsec latitude,37.5arcsec longitude), not upstream384x384; add1:1000 and1:500 with four-digit row/column; validate domestic unmerged A..N/43..53 domain and row/column ranges; implement southwest corner ownership with canonical rational-edge floats rather than epsilon/clamping.

Validation sources: GB/T13989-2012 sections2.2/2.3, table2,3.2/3.3 and appendixB. Official catalog status was read at https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=30799A9AF9BFA98E77D21ABC5F0984E2 . Official fulltext preview was unavailable in the HTTP reader; relevant original standard text was read from the facsimile https://www.scribd.com/document/934172840/ . A revision plan is not the current standard. The adapter does not implement high-latitude merged sheets, southern hemisphere prefixes, square/rectangular engineering indexing or historical formats. Code numbers do not identify a datum.

Independent tests use all11 appendixB examples, southwest edges and adjacent floating values, invalid codes, polygon holes and explicit touch semantics, with separate original native WT/WL frame save/reopen/output acceptance. Upstream four smoke tests are not a complete conformance proof.

