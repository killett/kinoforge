# Grid examples

Self-contained, commitable example grid YAMLs for `kinoforge grid`.
Every cfg + grid spec in this directory uses ONLY official, repo-verified
LoRA refs and prompts — safe to commit, unlike user-authored grid specs.

Each `.grid.yaml` opts in via `allow_in_repo: true` so
`GridSpec.load()`'s under-repo guard accepts it.

See the main `README.md` "Grid examples — verified" section for the
exact subshell + heredoc command per example. Schema details live in
`docs/batch-and-grid.md`.

Sub-dirs:
- `_fixtures/` — mp4s produced by earlier grid runs that mixed-path
  grids reference as `path:` cells. Committed; small (~1-5 MB each).
