"""Offline tests for C33 orchestrator helpers in tests/live/conftest.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

CONFTEST_PATH = Path(__file__).resolve().parents[1] / "live" / "conftest.py"
_spec = importlib.util.spec_from_file_location("c33_conftest", CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_c33_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c33_conftest)


class _FakeClient:
    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = list(scripted)
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append((query, dict(variables)))
        if not self._scripted:
            return {"data": {"pod": None}}
        return self._scripted.pop(0)


class _FakeS3:
    def list_objects_v2(self, **_kw: Any) -> dict[str, Any]:
        return {"Contents": [], "IsTruncated": False}


def test_c33_hard_cap_is_five_dollars() -> None:
    assert _c33_conftest.C33_HARD_CAP_USD == 5.00


def test_c33_sidecar_path_shape() -> None:
    p = _c33_conftest.c33_sidecar_path("p0")
    assert p.name == "_c33_probe_p0_evidence.json"


def test_c33_run_id_carries_phase_and_localtime_format() -> None:
    rid = _c33_conftest.c33_run_id("p0")
    assert rid.startswith("c33-p0-")
    suffix = rid.split("c33-p0-", 1)[1]
    assert len(suffix) == 15  # YYYYMMDDTHHMMSS
    assert suffix[8] == "T"
