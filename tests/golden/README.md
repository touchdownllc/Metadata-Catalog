# Golden artifact snapshots

These files are the frozen contract surface that scoring (and anything
else downstream of ingestion) will read. They are consumed by
`tests/test_artifact_snapshots.py` and
`tests/test_record_schema.py`.

Each `{state}_elements_{source,spine}.json` is a bit-for-bit copy of
`data/out/{state}_elements_{source,spine}.json` after a fresh full
pipeline run, with the run-timestamp (`extracted_at`) stripped. That
timestamp is the only non-deterministic field in the serialized shape —
stripping it lets the goldens diff cleanly.

Treat changes here like changes to a public API. The PR description
must explain *why* the drift is intentional, and reviewers should read
the JSON delta carefully.

## Regeneration ritual

From the repo root, with a fresh `data/out/` (run the four adapters
first via `mc ingest <state>` for each state):

```bash
scripts/refresh_goldens.py
```

The helper copies `data/out/{state}_elements_{lens}.json` → the matching
golden here, stripping `extracted_at` and normalizing trailing-newline.
It does not touch `data/out/` itself.

## What the snapshots do NOT cover

- `coverage_report_*.json`, `coverage_report_*.md`, `lens_divergence.*`,
  and the analyst XLSXs are validated by dedicated tests
  (`test_report_*.py`, `test_lens_consistency.py`). They are not frozen
  as goldens here to keep this surface small.
- Per-state XLSX / markdown outputs are not frozen — they are rendered
  from the JSON goldens, so any drift there surfaces downstream of
  these files.

## Size note

The TX source and spine goldens are the largest (~17-20 MB each)
because TEDS definition text is long. That's expected — the goldens
are the source of truth, not a sampled subset.
