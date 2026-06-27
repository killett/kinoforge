# Releasing kinoforge

(Moved from README §Releasing on 2026-06-27. See [../README.md](../README.md).)

## Releasing

Version bumps run through a single command so the pyproject pin can't
drift from the git tag (the v0.1.0/v0.4.0 mismatch surfaced during
Phase 50 closeout):

```
pixi run release 0.6.0
pixi run release 0.6.0 --note "graceful interrupt UX"
```

The helper refuses on dirty tree, existing tag, missing/duplicate
version line, or non-forward bump. On success it leaves one new commit
(`chore(release): bump version to X.Y.Z`) and one annotated tag
(`vX.Y.Z`) ready for `git push origin main --follow-tags`.

See `../tools/release.py` for the contract and `../tests/tools/test_release.py`
for the safety guards (multi-line abort, missing-line abort, semver
shape, forward-only bump).
