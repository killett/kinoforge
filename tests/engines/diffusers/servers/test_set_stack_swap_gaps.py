"""``/lora/set_stack`` swap-gap regression tests.

Spec: ``docs/superpowers/specs/2026-06-23-p2-swap-gap-design.md``.

Fences the 3 invariants the Tier-4 release-gate matrix's case_5 +
case_7 violated on 2026-06-23 (HEAD ``2a7d6f0``):

* T-A — Same-ref branch swap (2 refs, opposite branches): both files
  must survive on disk, inventory must end up keyed by the swapped
  target, per-transformer routing must hold. Pre-fix root cause:
  ``_evict_one`` unconditionally unlinked the shared file, AND the
  download-step pending-entry loop never ran because every ref was
  already downloaded → ``_replace_adapter_stack`` raised ``KeyError``
  outside the handler's ``(RuntimeError, ValueError)`` catch list →
  HTTP 500 on the live Tier-4 fire.
* T-B — Same ref in both branches (Q6 Option 1 composite identity):
  one download, two inventory rows, two distinct adapter names, one
  per-transformer activation per branch.
* T-C — Minimum-reproducer single-ref branch swap: the file underlying
  the only ref must NOT be unlinked when the only change is the branch
  field on a single inventory key.

Each test drives the FastAPI handler directly via
``asyncio.run(s.set_stack(req))`` mirroring the precedent in
``tests/engines/test_wan_t2v_server_set_stack.py``.

The MoE pipe stub is intentionally minimal — a per-transformer
recorder pair on a single object, plus ``load_lora_weights`` that
records the routing kwarg. We do NOT use ``MagicMock`` here because
the tests assert against the EXACT sequence of recorded calls and
``MagicMock``'s auto-attribute creation hides spelling errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import kinoforge.engines.diffusers.servers.wan_t2v_server as s


class _Recorder:
    """Per-transformer ``set_adapters`` recorder.

    Mirrors the recorder pattern in
    ``tests/smoke/local_cpu/stub_pipe.py`` so the test assertions
    speak the same language as Tier-1 local-CPU smoke fixtures.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[list[str], list[float]]] = []

    def set_adapters(
        self,
        names: list[str],
        adapter_weights: list[float] | None = None,
    ) -> None:
        self.calls.append((list(names), list(adapter_weights or [])))


class _MoEStub:
    """Wan-2.2-shape pipe stub: ``transformer`` + ``transformer_2``.

    ``load_lora_weights`` records ``(path, adapter_name, into_t2)`` so
    routing assertions are direct. ``delete_adapters`` records the
    adapter names that flow through eviction. ``unload_lora_weights``
    is a counted no-op.
    """

    def __init__(self) -> None:
        self.transformer = _Recorder("transformer")
        self.transformer_2 = _Recorder("transformer_2")
        self.loaded: list[tuple[str, str, bool]] = []
        self.deleted: list[str] = []
        self.unload_count: int = 0

    def load_lora_weights(
        self,
        path: str,
        adapter_name: str,
        load_into_transformer_2: bool = False,
    ) -> None:
        self.loaded.append((path, adapter_name, load_into_transformer_2))

    def delete_adapters(self, names: list[str]) -> None:
        self.deleted.extend(names)

    def unload_lora_weights(self) -> None:
        self.unload_count += 1


@pytest.fixture
def moe_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[Any, _MoEStub, list[str]]]:
    """Wan-2.2 MoE pipe stubbed in + fake download writes a real file.

    Yields ``(s, stub, download_log)`` so each test can seed inventory,
    drive the handler, and assert against the recorded routing.
    """
    s._inventory.clear()
    download_log: list[str] = []

    def _fake_download(spec: s.ArtifactDownloadSpec, dest_dir: Path) -> tuple[str, int]:
        download_log.append(spec.filename)
        target = dest_dir / spec.filename
        target.write_bytes(b"x" * (spec.size_hint or 100))
        return str(target), spec.size_hint or 100

    monkeypatch.setattr(s, "_download_one", _fake_download)
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 10_000_000)
    monkeypatch.setattr(s, "LORAS_DIR", tmp_path)

    stub = _MoEStub()
    monkeypatch.setattr(s, "pipe", stub)
    monkeypatch.setattr(s, "_pipe_arity", 2)
    yield s, stub, download_log
    s._inventory.clear()


def _seed_existing(
    tmp_path: Path,
    *,
    ref: str,
    branch: str,
    filename: str,
    size_bytes: int = 100,
    adapter_name: str = "",
) -> Path:
    """Seed ``s._inventory[(ref, branch)]`` AND write the on-disk file.

    Returns the on-disk path so the test can assert presence/absence
    after the handler runs.
    """
    file_path = tmp_path / filename
    file_path.write_bytes(b"x" * size_bytes)
    s._inventory[(ref, branch)] = {
        "ref": ref,
        "filename": filename,
        "size_bytes": size_bytes,
        "loras_dir_path": str(file_path),
        "downloaded_at_local": "2026-06-23T00:00:00",
        "last_used_at_local": "2026-06-23T00:00:00",
        "adapter_name": adapter_name,
        "last_strength": 1.0,
        "branch": branch,
    }
    return file_path


def _spec(filename: str, size_hint: int = 100) -> s.ArtifactDownloadSpec:
    return s.ArtifactDownloadSpec(
        url=f"https://example.invalid/{filename}",
        headers={},
        filename=filename,
        size_hint=size_hint,
    )


# ---------------------------------------------------------------------------
# T-A — two-ref branch swap (case_5 Tier-1 analog)
# ---------------------------------------------------------------------------


def test_two_ref_branch_swap_preserves_files_and_inventory(
    moe_server: tuple[Any, _MoEStub, list[str]], tmp_path: Path
) -> None:
    """Two existing refs swap branches; both files MUST survive.

    Sequence mirrors Tier-4 ``case_5``:
        current = {(HIGH, high_noise), (LOW, low_noise)}
        target  = {(HIGH, low_noise),  (LOW, high_noise)}

    Pre-fix bugs that this test catches:
      1. ``_evict_one`` (wan_t2v_server.py:574-602) unconditionally
         unlinks ``loras_dir_path`` — but both files are still needed
         under their new branch keys.
      2. The handler's pending-entry loop only runs for refs in
         ``to_download_refs`` (empty here, both already on disk) → no
         ``(HIGH, low_noise)`` / ``(LOW, high_noise)`` entries get
         created → ``_replace_adapter_stack`` raises ``KeyError`` on
         the inventory lookup → unmapped exception → HTTP 500.
    """
    s_mod, stub, download_log = moe_server

    high_file = _seed_existing(
        tmp_path, ref="HIGH", branch="high_noise", filename="high.safetensors"
    )
    low_file = _seed_existing(
        tmp_path, ref="LOW", branch="low_noise", filename="low.safetensors"
    )

    req = s_mod.SetStackRequest(
        target=[
            {"ref": "HIGH", "strength": 1.0, "branch": "low_noise"},
            {"ref": "LOW", "strength": 1.0, "branch": "high_noise"},
        ],
        download_specs={
            "HIGH": _spec("high.safetensors"),
            "LOW": _spec("low.safetensors"),
        },
    )
    resp = asyncio.run(s_mod.set_stack(req))

    assert resp.swap_rejected is None, (
        f"swap unexpectedly rejected: {resp.swap_rejected}"
    )
    assert high_file.exists(), (
        "HIGH file unlinked — _evict_one ignored the surviving "
        "(HIGH, low_noise) sibling"
    )
    assert low_file.exists(), (
        "LOW file unlinked — _evict_one ignored the surviving (LOW, high_noise) sibling"
    )

    inv_keys = {(row.ref, row.branch) for row in resp.inventory}
    assert inv_keys == {("HIGH", "low_noise"), ("LOW", "high_noise")}, (
        f"inventory keys not swapped to target: {inv_keys}"
    )

    assert download_log == [], (
        f"unexpected re-download — both files were on disk: {download_log}"
    )

    routing_by_name = {name: into_t2 for _path, name, into_t2 in stub.loaded}
    assert routing_by_name == {"lora_0_l": True, "lora_1_h": False}, (
        f"per-transformer routing wrong after swap: {stub.loaded}"
    )

    transformer_names = [names for names, _w in stub.transformer.calls]
    transformer_2_names = [names for names, _w in stub.transformer_2.calls]
    assert transformer_names == [["lora_1_h"]], (
        f"transformer activation wrong: {stub.transformer.calls}"
    )
    assert transformer_2_names == [["lora_0_l"]], (
        f"transformer_2 activation wrong: {stub.transformer_2.calls}"
    )


# ---------------------------------------------------------------------------
# T-B — same ref in both branches (case_7 Tier-1 analog, fresh state)
# ---------------------------------------------------------------------------


def test_same_ref_two_branches_yields_two_inventory_rows(
    moe_server: tuple[Any, _MoEStub, list[str]],
) -> None:
    """Composite-identity contract — same ref under both branches.

    Sequence mirrors Tier-4 ``case_7`` in fresh state:
        current = {}
        target  = {(HIGH, high_noise, s=1.0), (HIGH, low_noise, s=0.8)}

    Fences Q6 Option 1: ``(ref, branch)`` is the inventory key, NOT
    ``ref`` alone. A future refactor that re-keys inventory by ref
    would collapse the second row and silently drop the low_noise
    activation.

    Also fences the download-dedup invariant: one ``_download_one``
    call serves both branches.
    """
    s_mod, stub, download_log = moe_server

    req = s_mod.SetStackRequest(
        target=[
            {"ref": "HIGH", "strength": 1.0, "branch": "high_noise"},
            {"ref": "HIGH", "strength": 0.8, "branch": "low_noise"},
        ],
        download_specs={"HIGH": _spec("high.safetensors")},
    )
    resp = asyncio.run(s_mod.set_stack(req))

    assert resp.swap_rejected is None, (
        f"swap unexpectedly rejected: {resp.swap_rejected}"
    )

    assert download_log == ["high.safetensors"], (
        f"download dedup broken — expected one download, got {download_log}"
    )

    keys = sorted((row.ref, row.branch) for row in resp.inventory)
    assert keys == [("HIGH", "high_noise"), ("HIGH", "low_noise")], (
        f"composite-key inventory wrong: {keys}"
    )

    strength_by_branch = {row.branch: row.last_strength for row in resp.inventory}
    assert strength_by_branch == {"high_noise": 1.0, "low_noise": 0.8}, (
        f"per-branch strength wrong: {strength_by_branch}"
    )

    adapter_by_branch = {row.branch: row.adapter_name for row in resp.inventory}
    assert adapter_by_branch == {"high_noise": "lora_0_h", "low_noise": "lora_1_l"}, (
        f"adapter naming collision across branches: {adapter_by_branch}"
    )

    routing_by_name = {name: into_t2 for _path, name, into_t2 in stub.loaded}
    assert routing_by_name == {"lora_0_h": False, "lora_1_l": True}, (
        f"per-transformer routing wrong on composite: {stub.loaded}"
    )

    assert [names for names, _w in stub.transformer.calls] == [["lora_0_h"]], (
        f"transformer activation should fire once with [lora_0_h]: "
        f"{stub.transformer.calls}"
    )
    assert [names for names, _w in stub.transformer_2.calls] == [["lora_1_l"]], (
        f"transformer_2 activation should fire once with [lora_1_l]: "
        f"{stub.transformer_2.calls}"
    )

    assert [weights for _n, weights in stub.transformer.calls] == [[1.0]]
    assert [weights for _n, weights in stub.transformer_2.calls] == [[0.8]]


# ---------------------------------------------------------------------------
# T-C — single-ref branch swap (minimum reproducer for _evict_one fix)
# ---------------------------------------------------------------------------


def test_single_ref_branch_swap_keeps_file_on_disk(
    moe_server: tuple[Any, _MoEStub, list[str]], tmp_path: Path
) -> None:
    """Minimum reproducer — one ref, branch swap, file MUST survive.

    Sequence:
        current = {(HIGH, high_noise)}    # one file on disk
        target  = {(HIGH, low_noise)}

    Pre-fix bug: ``_evict_one(\"HIGH\", \"high_noise\")`` unlinks
    ``high.safetensors`` because the surviving-entry check did not
    exist; then the handler tries to load from the now-missing path
    and fails. The mandatory_evict bookkeeping is fine here — the
    bug is purely the file unlink.
    """
    s_mod, stub, download_log = moe_server

    high_file = _seed_existing(
        tmp_path,
        ref="HIGH",
        branch="high_noise",
        filename="high.safetensors",
    )

    req = s_mod.SetStackRequest(
        target=[{"ref": "HIGH", "strength": 1.0, "branch": "low_noise"}],
        download_specs={"HIGH": _spec("high.safetensors")},
    )
    resp = asyncio.run(s_mod.set_stack(req))

    assert resp.swap_rejected is None
    assert high_file.exists(), (
        "HIGH file unlinked — single-ref branch swap deleted the underlying artifact"
    )
    assert download_log == [], (
        f"single-ref branch swap should not re-download: {download_log}"
    )

    keys = {(row.ref, row.branch) for row in resp.inventory}
    assert keys == {("HIGH", "low_noise")}, (
        f"inventory should reflect swapped branch: {keys}"
    )

    routing_by_name = {name: into_t2 for _path, name, into_t2 in stub.loaded}
    assert routing_by_name == {"lora_0_l": True}, (
        f"single-ref swap routing wrong: {stub.loaded}"
    )
