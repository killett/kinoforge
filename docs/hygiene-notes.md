# Hygiene notes — intentionally kept smells

This file records code smells that periodic hygiene passes have evaluated and
deliberately **left in place**, with the reasoning. Future passes should
consult this file before re-flagging the same items.

Entries here are NOT bugs and NOT TODOs — they are conscious "leave it"
decisions, captured so we do not re-litigate them on every pass.

---

## Duplicated provision branch in `core/orchestrator.py`

**Location:** `src/kinoforge/core/orchestrator.py` — the cache-miss
("discovery") branch and the post-cache-hit branch each contain an
`if resolved_engine.requires_compute: ... else: backend = resolved_engine.backend(None, cfg_dict)`
block that differs only in passing `for_discovery=True` vs
`for_discovery=False` to `_provision_instance_and_build_backend`.

**Why kept:** the two blocks live inside a non-trivial control flow that
already has clear step-numbered comments and distinct preconditions
(`ProfileNotCached` vs `backend is None`). Extracting a small helper is
behavior-preserving in principle but moves a load-bearing branch behind one
more layer of indirection. The duplication is local (single function) and
cheap to read. Reconsider if a third caller appears or if the branches start
diverging.
