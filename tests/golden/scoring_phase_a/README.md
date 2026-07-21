# Phase A scoring goldens

Frozen contract for the Phase A binary-fact extraction harness
(`mc score extract --fact has_conditional_logic --state AZ --lens spine
--dry-run --limit 200`). Two pieces:

- `az_dry_run_manifest.json` — the dry-run manifest: one entry per entity
  batch with `element_names`, `prompt_file`, and the SHA-256 cache key.
  Drift here means the prompt text, element ordering, or cache-key
  derivation changed.
- `prompts/*.prompt.md` — one file per entity batch, byte-identical to
  what the LLM would see under the API. Drift here means the prompt
  template, entity ordering, or element formatting changed.

## Regeneration ritual

From the repo root, after an intentional change to the prompt template
or the Phase A filter/sort logic:

```bash
.venv/bin/python scripts/refresh_goldens_phase_a.py
```

The helper shells out to `mc score extract --dry-run` against the
current `data/out/az_elements_spine.json`, then copies the rendered
prompts + manifest over these files. It does not touch the live
`data/out/` artifact.

## What the goldens do NOT cover

- The live `.jsonl` artifact produced by a non-`--dry-run` run.
  Byte-identical replay of that file is asserted in a separate test
  that pre-seeds the cache with synthetic responses.
- Response-side shape. That lives in `tests/test_score_phase_a.py` via
  the JSON Schema contract.

Treat any drift here like a public-API change — the PR description must
explain *why* the prompt or batching changed, and reviewers should read
the JSON delta carefully.
