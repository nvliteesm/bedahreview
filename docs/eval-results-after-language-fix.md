# Evaluation results

Model: `gemini-3.5-flash-lite`. Run on 2026-10-28. Scored 100 of 100 labelled reviews (0 failed to classify).

> **How to read this.** The labels in the sheet are drafts (written with Claude's help, and the prompt was tuned on the same rows), and the fix/fight bucket is derived from them (`gold_bucket` in `evaluate.py`). So this is **agreement with the sheet, not truth**, and it is probably optimistic. Run-to-run noise is a few points.

Sheet rows by derived bucket: fix 54, fight 12, ignore 0, keep 34. Buckets with no rows (ignore) cannot be scored.

## Headline

| Measure | Result |
|---|---|
| Bucket accuracy (all five model buckets) | 88% |
| Accuracy on reviews the model committed to (not set aside as check) | 94% |
| Theme accuracy | 81% |
| Sheet-fixable reviews that got a fix suggestion | 100% |

## Per bucket

| Bucket | Precision | Recall | Reviews in sheet |
|---|---|---|---|
| fix | 94% | 91% | 54 |
| fight | 89% | 67% | 12 |
| ignore | n/a | n/a | 0 |
| keep | 94% | 91% | 34 |

## Confusion matrix

Rows are the sheet's bucket, columns are the model's.

| sheet → model | fix | fight | ignore | keep | check |
|---|---|---|---|---|---|
| **fix** | 49 | 0 | 0 | 0 | 5 |
| **fight** | 2 | 8 | 0 | 2 | 0 |
| **ignore** | 0 | 0 | 0 | 0 | 0 |
| **keep** | 1 | 1 | 0 | 31 | 1 |

## Likely-fake and courier flags

| Flag | Precision | Recall | TP | FP | FN |
|---|---|---|---|---|---|
| Likely fake | 100% | 100% | 5 | 0 | 0 |
| Courier, check first | 25% | 50% | 2 | 6 | 2 |

Few rows are marked fake in the sheet, so those percentages rest on very small numbers.

## The 10 most important disagreements

12 disagreements in total. Look up each # in the sheet and write your verdict in the last column (was the sheet wrong, or the model?). Review text is left out on purpose.

| # | Sheet says | Model says | Sheet theme | Model theme | Model's cause | Your comment |
|---|---|---|---|---|---|---|
| 39 | fight | fix | Spoilage / freshness | Spoilage / freshness | product |  |
| 79 | fight | fix | Packaging / damaged on arrival | Packaging / damaged on arrival | packing |  |
| 32 | keep | fix | Praise: quality / material | Defect / not working | packing |  |
| 47 | keep | fight | Praise: taste | Taste / subjective | buyer |  |
| 52 | fight | keep | Taste / subjective | Taste / subjective | none |  |
| 53 | fight | keep | Taste / subjective | Taste / subjective | none |  |
| 24 | fix | check | Fabric & material quality | Not as described / claim vs reality | unclear |  |
| 3 | fix | check | Fake / authenticity doubt | Not as described / claim vs reality | unclear |  |
| 46 | keep | check | Praise: taste | Taste / subjective | unclear |  |
| 5 | fix | check | Not as described / claim vs reality | Not as described / claim vs reality | unclear |  |
