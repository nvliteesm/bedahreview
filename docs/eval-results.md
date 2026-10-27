# Evaluation results

Model: `gemini-3.5-flash-lite`. Run on 2026-10-27. Scored 100 of 100 labelled reviews (0 failed to classify).

> **How to read this.** The labels in the sheet are drafts (written with Claude's help, and the prompt was tuned on the same rows), and the fix/fight bucket is derived from them (`gold_bucket` in `evaluate.py`). So this is **agreement with the sheet, not truth**, and it is probably optimistic. Run-to-run noise is a few points.

Sheet rows by derived bucket: fix 54, fight 12, ignore 0, keep 34. Buckets with no rows (ignore) cannot be scored.

## Headline

| Measure | Result |
|---|---|
| Bucket accuracy (all five model buckets) | 86% |
| Accuracy on reviews the model committed to (not set aside as check) | 91% |
| Theme accuracy | 81% |
| Sheet-fixable reviews that got a fix suggestion | 98% |

## Per bucket

| Bucket | Precision | Recall | Reviews in sheet |
|---|---|---|---|
| fix | 89% | 89% | 54 |
| fight | 100% | 58% | 12 |
| ignore | n/a | n/a | 0 |
| keep | 91% | 91% | 34 |

## Confusion matrix

Rows are the sheet's bucket, columns are the model's.

| sheet → model | fix | fight | ignore | keep | check |
|---|---|---|---|---|---|
| **fix** | 48 | 0 | 0 | 1 | 5 |
| **fight** | 3 | 7 | 0 | 2 | 0 |
| **ignore** | 0 | 0 | 0 | 0 | 0 |
| **keep** | 3 | 0 | 0 | 31 | 0 |

## Likely-fake and courier flags

| Flag | Precision | Recall | TP | FP | FN |
|---|---|---|---|---|---|
| Likely fake | 100% | 100% | 5 | 0 | 0 |
| Courier, check first | 100% | 50% | 2 | 0 | 2 |

Few rows are marked fake in the sheet, so those percentages rest on very small numbers.

## The 10 most important disagreements

14 disagreements in total. Look up each # in the sheet and write your verdict in the last column (was the sheet wrong, or the model?). Review text is left out on purpose.

| # | Sheet says | Model says | Sheet theme | Model theme | Model's cause | Your comment |
|---|---|---|---|---|---|---|
| 39 | fight | fix | Spoilage / freshness | Spoilage / freshness | product |  |
| 49 | fight | fix | Taste / subjective | Taste / subjective | product |  |
| 79 | fight | fix | Packaging / damaged on arrival | Packaging / damaged on arrival | packing |  |
| 23 | fix | keep | Fabric & material quality | Praise: quality / material | none |  |
| 32 | keep | fix | Praise: quality / material | Fabric & material quality | packing |  |
| 46 | keep | fix | Praise: taste | Taste / subjective | product |  |
| 47 | keep | fix | Praise: taste | Taste / subjective | product |  |
| 52 | fight | keep | Taste / subjective | Praise: taste | none |  |
| 53 | fight | keep | Taste / subjective | Praise: taste | none |  |
| 3 | fix | check | Fake / authenticity doubt | Not as described / claim vs reality | unclear |  |
