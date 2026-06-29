"""Live evidence smoke for CivArchiveSource (sub-project B).

One $0 anonymous HTTP GET against civarchive.com. Confirms the pinned
HTML fixture is faithful to the live page shape on the smoke date.

Run from repo root:

    pixi run python tests/live/evidence/2026-06-28-civarchive-source/resolve.py

Writes two files alongside this script:
    response_meta.json  — HTTP status + response headers + timestamp
    evidence.md         — human-readable summary + verdict
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from datetime import datetime
from urllib.request import Request, urlopen

HERE = pathlib.Path(__file__).resolve().parent
# Repo root needed when running from a fresh interpreter without
# pixi-activated PYTHONPATH; harmless when kinoforge is already importable.
sys.path.insert(0, str(HERE.parents[3] / "src"))

from kinoforge.core.credentials import EnvCredentialProvider  # noqa: E402
from kinoforge.sources.civarchive import CivArchiveSource  # noqa: E402

REF = "civarchive:2197303@2474081"
PAGE_URL = "https://civarchive.com/models/2197303?modelVersionId=2474081"
EXPECTED_SHA256 = "67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d"
EXPECTED_FILENAME = "wan2.2_t2v_arcanestyle_high.safetensors"


def main() -> int:
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")

    # 1) Live HTML fetch (captured for response_meta).
    req = Request(PAGE_URL, headers={"User-Agent": "kinoforge-smoke/0.1"})  # noqa: S310
    with urlopen(req) as resp:  # noqa: S310 — public civarchive URL
        status = resp.status
        content_type = resp.headers.get("Content-Type", "")
        body = resp.read()
    content_length = len(body)

    # 2) Resolve via the production CivArchiveSource (with default fetch).
    src = CivArchiveSource()
    creds = EnvCredentialProvider()
    artifacts = src.resolve(REF, creds)
    assert len(artifacts) == 1
    artifact = artifacts[0]

    # 3) Verdict — compare live values against pinned fixture expectations.
    pass_sha = artifact.sha256 == EXPECTED_SHA256
    pass_name = artifact.filename == EXPECTED_FILENAME
    verdict = "PASS" if pass_sha and pass_name else "FIXTURE-DRIFT"

    # 4) Persist response_meta.json.
    (HERE / "response_meta.json").write_text(
        json.dumps(
            {
                "status": status,
                "content_type": content_type,
                "content_length": content_length,
                "fetched_at": fetched_at,
            },
            indent=2,
        )
        + "\n"
    )

    # 5) Persist evidence.md.
    art_repr = json.dumps(dataclasses.asdict(artifact), indent=2)
    (HERE / "evidence.md").write_text(
        f"""# CivArchive source — live evidence

**Date:** {fetched_at}
**Ref:** `{REF}`
**Fetched URL:** `{PAGE_URL}`
**HTTP status:** {status}
**Response Content-Type:** `{content_type}`
**Body length:** {content_length} bytes

## Resolved Artifact

```json
{art_repr}
```

## Verdict

**{verdict}**

- sha256 == pinned fixture: {pass_sha}
- filename == pinned fixture: {pass_name}

Pinned fixture: `tests/sources/civarchive/fixtures/version_2474081.html`.
If verdict is `FIXTURE-DRIFT`, the fixture is stale: refresh it and
re-run unit tests before relying on this evidence.
"""
    )

    print(f"Verdict: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
