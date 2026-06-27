# Output directory layout

(Moved from README §Output directory (incl. Configuring it and --run-id change) on 2026-06-27. See [../README.md](../README.md).)

## Output directory

Final clips publish to a flat user-visible directory (default `output/` at
the repo root) with filenames of the form:

    YYYYMMDD-HHMMSS_<prompt-slug>.<ext>

* The timestamp is local-TZ at the moment the clip finishes.
* The slug is the first 20 ASCII-safe characters of the prompt; emoji,
  CJK, accented characters, and punctuation are dropped (the slug
  pipeline is ASCII-conservative for cross-platform safety and
  grep/tab-complete ergonomics).
* Collisions in the same second resolve as `_2`, `_3`, … `_99`, then a
  6-character sha256 hash.
* Batch entries nest under `output/<batch_id>/` for grouping.

The internal artifact store (profile cache, ledger, weights cache,
intermediate segment artifacts) is unchanged — it still lives under
`--state-dir` (default `.kinoforge/`) and is operator-facing, not
user-facing. The output dir is a *publish* target, not a replacement
for the store.

### Configuring it

YAML block (optional; absent block uses the defaults below):

```yaml
output:
  kind: local            # only "local" ships in v1
  dir: output            # relative-to-cwd, or absolute
  enabled: true          # set false to skip publishing
```

CLI flags (overrides YAML):

* `--output-dir PATH` — publish here instead of the YAML default.
* `--no-output-dir` — skip publishing for this invocation.
* Flags are mutually exclusive.

### `--run-id` change

The `kinoforge generate --run-id` default changed from the literal
string `"run"` to `f"run-{YYYYMMDD-HHMMSS}"` (local TZ at invocation
time). This closes a silent-overwrite foot-gun where two successive
`kinoforge generate` calls without explicit `--run-id` would overwrite
each other's internal artifact + ledger entry. Pass `--run-id run` to
restore the prior behavior verbatim. Batch runs are unaffected — each
manifest entry already names its own `run_id`.
