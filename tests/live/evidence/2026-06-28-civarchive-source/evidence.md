# CivArchive source — live evidence

**Date:** 2026-06-28T22:35:49-07:00
**Ref:** `civarchive:2197303@2474081`
**Fetched URL:** `https://civarchive.com/models/2197303?modelVersionId=2474081`
**HTTP status:** 200
**Response Content-Type:** `text/html; charset=utf-8`
**Body length:** 71210 bytes

## Resolved Artifact

```json
{
  "filename": "wan2.2_t2v_arcanestyle_high.safetensors",
  "url": "https://civarchive.com/api/download/models/2474081",
  "size": null,
  "sha256": "67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d",
  "headers": {},
  "uri": "",
  "meta": {}
}
```

## Verdict

**PASS**

- sha256 == pinned fixture: True
- filename == pinned fixture: True

Pinned fixture: `tests/sources/civarchive/fixtures/version_2474081.html`.
If verdict is `FIXTURE-DRIFT`, the fixture is stale: refresh it and
re-run unit tests before relying on this evidence.
