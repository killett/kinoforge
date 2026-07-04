# E21 — keyframe asset hand-off to hosted video engines (data-URI inlining)

**Date:** 2026-07-04 (PDT)
**Status:** validated (autonomous session — user-gate waived per standing
autonomy memory; Dr. Twinklebrane reviews post-hoc)
**Closes:** Phase 32 deferred item E21 ("fal storage upload integration
for keyframe→wan i2v / flf2v end-to-end"). Unblocks the deferred
Phase 43 Task 14 (fal i2v/flf2v extension) path and makes
`examples/configs/keyframe-luma.yaml` runnable end-to-end.

## 1. Problem

`KeyframeStage` stores the generated keyframe in the LOCAL artifact
store; `ConditioningAsset.ref.uri` is a bare filesystem path
(`LocalArtifactStore.put_bytes` → `Artifact(uri=str(p))`). The fal
backend writes that uri verbatim into the request body at the
configured `asset_paths` dot-path (`image_url`) — fal's servers cannot
fetch `/workspace/.kinoforge/...`, so every keyframe→i2v run dies at
submit.

## 2. Approaches

- **A (chosen): data-URI inlining at the fal injection seam.** When the
  asset uri has no URL scheme (or `file:`), read the bytes and write
  `data:image/<ext>;base64,...` instead. fal's `*_url` inputs
  officially accept Base64 data URIs. Zero new infra, no auth, no
  upload lifecycle. Cost: request body grows ~4/3 × image size — a
  5.9 MB Luma PNG becomes ~7.9 MB JSON, within fal's request limits;
  log a warning above 8 MB so oversize failures are diagnosable.
- B: fal storage upload API (initiate/PUT/finalise flow) — real
  hosted URL, smaller bodies, but a 3-step authenticated upload
  lifecycle for a problem A solves in ~20 lines. Revisit only if data
  URIs hit size limits in practice.
- C: pass the image provider's own URL through (Luma pre-signed S3) —
  expires in 1 h and is provider-specific; rejected.

## 3. Change surface

- `src/kinoforge/engines/fal/__init__.py` — in `submit()`'s asset
  loop: `set_by_dot_path(body, dot_path, _asset_uri_for_wire(asset))`
  where the new module-level helper returns the uri unchanged for
  `http(s):`/`data:` schemes and inlines local paths (and `file:`
  URIs) as data URIs. Mime from suffix: `.png` → `image/png`,
  `.jpg/.jpeg` → `image/jpeg`, anything else → `application/octet-stream`.
  Missing local file → `ValidationError` naming the role + path
  (fail at submit, not with an opaque fal-side 422).
- `examples/configs/keyframe-luma.yaml` — add the missing
  `asset_paths: {init_image: image_url}` to the fal engine block (the
  cfg as shipped never actually delivered the keyframe).
- Same fix applies to `keyframe-fal-i2v.yaml` / `keyframe-fal-flf2v.yaml`
  users automatically — the seam is in the backend, not the cfg.

## 4. Testing

- `tests/engines/test_fal.py` additions (or sibling new file if that
  file is oversized): local-path asset → body carries
  `data:image/png;base64,` prefix + round-trips back to the original
  bytes; `https:` uri passes through untouched; missing file raises
  `ValidationError` naming the role; `.jpg` gets `image/jpeg`.
- Live (optional, budget-gated): full
  `kinoforge generate --config keyframe-luma.yaml --mode i2v` — Luma
  UNI-1 keyframe → fal wan-i2v clip. New capability tuple
  (keyframe pre-stage + hosted i2v). ~$0.2-0.5 fal spend.

## 5. Out of scope

- E22 (roles beyond `init_image`), fal storage upload (approach B),
  data-URI support for other hosted engines (replicate/runway follow
  the same pattern when their keyframe flows land).
