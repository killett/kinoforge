"""Behavior: the shared apply sequence — ordering, rollback, replace.

These are the invariants a per-model reimplementation gets wrong. A half-applied
stack that still reports the full inventory is the worst outcome available: the
controller believes a LoRA is active that is not.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kinoforge.engines.diffusers.servers import _lora


class FakeModule:
    """Stands in for a transformer; records set_adapters calls."""

    def __init__(self) -> None:
        self.adapter_calls: list[tuple[list[str], list[float]]] = []
        self.dtype_calls: list[Any] = []

    def set_adapters(self, names: list[str], weights: list[float]) -> None:
        self.adapter_calls.append((list(names), list(weights)))

    def to(self, dtype: Any) -> FakeModule:
        self.dtype_calls.append(dtype)
        return self


class FakePipe:
    """Records load/unload ordering the way the real mixin would see it.

    ``log`` is the SHARED ordering record: loads and the profile's after_load
    both append to it, so a test can pin that after_load ran after every load
    and not merely that it ran once.
    """

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.module = FakeModule()
        self.loaded: list[tuple[str, str, str]] = []
        self.log: list[str] = []
        self.unload_count = 0
        self._fail_on = fail_on

    def unload_lora_weights(self) -> None:
        self.unload_count += 1
        self.loaded.clear()

    def load(self, path: str, adapter_name: str, target: str) -> None:
        if self._fail_on is not None and self._fail_on in path:
            raise RuntimeError("size mismatch for blocks.0.attn.out_proj.lora_B")
        self.loaded.append((path, adapter_name, target))
        self.log.append(f"load:{adapter_name}")


def _profile(
    pipe: FakePipe, *, after_load: Callable[[Any], None] | None = None
) -> _lora.LoraProfile:
    return _lora.LoraProfile(
        name="fake",
        targets=("transformer",),
        default_target="transformer",
        load=lambda p, path, adapter_name, target: p.load(path, adapter_name, target),
        module_for=lambda p, target: p.module,
        after_load=after_load or (lambda p: p.log.append("after_load")),
        explain_load_failure=lambda exc: (
            "pruned-checkpoint hint" if "size mismatch" in str(exc) else None
        ),
    )


@pytest.fixture(autouse=True)
def _clean_inventory() -> None:
    """Module-level inventory is process state — no test may inherit another's."""
    _lora._INVENTORY.clear()


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    paths = {}
    for name in ("a.safetensors", "b.safetensors"):
        p = tmp_path / name
        p.write_bytes(b"x" * 16)
        paths[name] = p
    return paths


def test_apply_loads_in_order_with_positional_adapter_names(
    files: dict[str, Path],
) -> None:
    """Order is the activation order; names are positional and stable."""
    pipe = FakePipe()
    inv = _lora.apply_stack(
        pipe,
        _profile(pipe),
        entries=[
            _lora.ResolvedEntry(
                ref="r:a", path=files["a.safetensors"], strength=0.8, target=None
            ),
            _lora.ResolvedEntry(
                ref="r:b", path=files["b.safetensors"], strength=0.4, target=None
            ),
        ],
    )
    assert [n for _p, n, _t in pipe.loaded] == ["lora_0", "lora_1"]
    assert pipe.module.adapter_calls == [(["lora_0", "lora_1"], [0.8, 0.4])]
    assert [e.ref for e in inv] == ["r:a", "r:b"]


def test_after_load_runs_once_after_every_entry(files: dict[str, Path]) -> None:
    """The dtype restore runs ONCE and AFTER the last load — not per-entry.

    Loads and after_load share one log, so hoisting after_load above the loop
    (which the 'exactly once' half of the criterion cannot see) fails here too.
    """
    pipe = FakePipe()
    _lora.apply_stack(
        pipe,
        _profile(pipe),
        entries=[
            _lora.ResolvedEntry(
                ref="r:a", path=files["a.safetensors"], strength=1.0, target=None
            ),
            _lora.ResolvedEntry(
                ref="r:b", path=files["b.safetensors"], strength=1.0, target=None
            ),
        ],
    )
    assert pipe.log == ["load:lora_0", "load:lora_1", "after_load"]


def test_failure_midway_rolls_back_to_empty(files: dict[str, Path]) -> None:
    """Entry 2 of 2 fails -> nothing stays loaded, and the error carries the hint.

    Catches a mid-loop break that leaves adapter lora_0 live while the caller
    believes the whole stack applied.
    """
    pipe = FakePipe(fail_on="b.safetensors")
    with pytest.raises(_lora.LoraLoadError) as excinfo:
        _lora.apply_stack(
            pipe,
            _profile(pipe),
            entries=[
                _lora.ResolvedEntry(
                    ref="r:a", path=files["a.safetensors"], strength=1.0, target=None
                ),
                _lora.ResolvedEntry(
                    ref="r:b", path=files["b.safetensors"], strength=1.0, target=None
                ),
            ],
        )
    assert excinfo.value.hint == "pruned-checkpoint hint"
    assert excinfo.value.ref == "r:b"
    assert pipe.loaded == []
    assert pipe.unload_count == 2  # once at entry, once rolling back


def test_second_apply_replaces_rather_than_accumulates(files: dict[str, Path]) -> None:
    """Applying [A] then [B] leaves exactly [B].

    Catches a skipped unload, where A and B blend silently while the inventory
    still reads [B].
    """
    pipe = FakePipe()
    profile = _profile(pipe)
    _lora.apply_stack(
        pipe,
        profile,
        entries=[
            _lora.ResolvedEntry(
                ref="r:a", path=files["a.safetensors"], strength=1.0, target=None
            )
        ],
    )
    inv = _lora.apply_stack(
        pipe,
        profile,
        entries=[
            _lora.ResolvedEntry(
                ref="r:b", path=files["b.safetensors"], strength=1.0, target=None
            )
        ],
    )
    assert [e.ref for e in inv] == ["r:b"]
    assert [p for p, _n, _t in pipe.loaded] == [str(files["b.safetensors"])]
    assert files["a.safetensors"].exists()


def test_missing_default_target_is_refused_with_legal_values() -> None:
    """A two-partition profile with no default must not guess.

    This is diffusers' documented H3 hazard: the wrong partition loads fine and
    degrades output with no error anywhere.
    """
    pipe = FakePipe()
    profile = _lora.LoraProfile(
        name="dual",
        targets=("transformer", "transformer_ref"),
        default_target=None,
        load=lambda p, path, adapter_name, target: None,
        module_for=lambda p, target: p.module,
        after_load=lambda p: None,
        explain_load_failure=lambda exc: None,
    )
    with pytest.raises(_lora.TargetRequired, match="transformer_ref"):
        _lora.apply_stack(
            pipe,
            profile,
            entries=[
                _lora.ResolvedEntry(
                    ref="r:a", path=Path("/tmp/x"), strength=1.0, target=None
                )
            ],
        )


def test_per_target_grouping_calls_set_adapters_once_per_module(
    files: dict[str, Path],
) -> None:
    """peft raises on an unknown adapter name, so each module gets only its own.

    Catches a single pipe-wide set_adapters carrying every name, which blows up
    on the second module of a dual-partition model (H3's transformer_ref).
    """
    pipe = FakePipe()
    modules = {"transformer": FakeModule(), "transformer_ref": FakeModule()}
    profile = _lora.LoraProfile(
        name="dual",
        targets=("transformer", "transformer_ref"),
        default_target="transformer",
        load=lambda p, path, adapter_name, target: p.load(path, adapter_name, target),
        module_for=lambda p, target: modules[target],
        after_load=lambda p: None,
        explain_load_failure=lambda exc: None,
    )
    _lora.apply_stack(
        pipe,
        profile,
        entries=[
            _lora.ResolvedEntry(
                ref="r:a", path=files["a.safetensors"], strength=0.7, target=None
            ),
            _lora.ResolvedEntry(
                ref="r:b",
                path=files["b.safetensors"],
                strength=0.3,
                target="transformer_ref",
            ),
        ],
    )
    assert modules["transformer"].adapter_calls == [(["lora_0"], [0.7])]
    assert modules["transformer_ref"].adapter_calls == [(["lora_1"], [0.3])]


def test_inventory_snapshot_tracks_the_live_stack(files: dict[str, Path]) -> None:
    """The snapshot is what the router reports; a rollback must empty it.

    Catches the worst available outcome: entry 0 loaded, entry 1 failed, and
    /lora/inventory still advertising both.
    """
    pipe = FakePipe()
    _lora.apply_stack(
        pipe,
        _profile(pipe),
        entries=[
            _lora.ResolvedEntry(
                ref="r:a", path=files["a.safetensors"], strength=0.5, target=None
            )
        ],
    )
    snap = _lora.inventory_snapshot()
    assert [(e.ref, e.adapter_name, e.strength, e.target) for e in snap] == [
        ("r:a", "lora_0", 0.5, "transformer")
    ]
    assert snap[0].filename == "a.safetensors"
    assert snap[0].size_bytes == 16

    failing = FakePipe(fail_on="b.safetensors")
    with pytest.raises(_lora.LoraLoadError):
        _lora.apply_stack(
            failing,
            _profile(failing),
            entries=[
                _lora.ResolvedEntry(
                    ref="r:a", path=files["a.safetensors"], strength=1.0, target=None
                ),
                _lora.ResolvedEntry(
                    ref="r:b", path=files["b.safetensors"], strength=1.0, target=None
                ),
            ],
        )
    assert _lora.inventory_snapshot() == []


def test_failing_after_load_also_unloads(files: dict[str, Path]) -> None:
    """Rollback covers every step that can leave an adapter attached, not just loads.

    load_lora_weights attaches an ACTIVE adapter. If after_load raises and the
    pod does not unload, it generates WITH the LoRA — at weight 1.0, since
    set_adapters never ran — while /lora/inventory reports nothing loaded.
    """
    pipe = FakePipe()

    def boom(_pipe: Any) -> None:
        raise RuntimeError("bf16 restore failed")

    with pytest.raises(RuntimeError, match="bf16 restore failed"):
        _lora.apply_stack(
            pipe,
            _profile(pipe, after_load=boom),
            entries=[
                _lora.ResolvedEntry(
                    ref="r:a", path=files["a.safetensors"], strength=1.0, target=None
                )
            ],
        )
    assert pipe.unload_count == 2  # once at entry, once rolling back
    assert pipe.loaded == []
    assert _lora.inventory_snapshot() == []


def test_a_file_vanishing_before_publish_rolls_back(files: dict[str, Path]) -> None:
    """A row that cannot be built must not publish the rows before it.

    The LoRA dir is shared and Wan already runs LRU eviction over it, so a file
    CAN disappear between load and stat. Catches both a partial inventory and a
    pipeline left holding the full weighted stack under an empty one.
    """
    pipe = FakePipe()

    def load_then_evict(p: Any, path: str, adapter_name: str, target: str) -> None:
        """Entry 1 loads fine, then its file is evicted out from under us."""
        p.load(path, adapter_name, target)
        if "b.safetensors" in path:
            files["b.safetensors"].unlink()

    profile = _lora.LoraProfile(
        name="fake",
        targets=("transformer",),
        default_target="transformer",
        load=load_then_evict,
        module_for=lambda p, target: p.module,
        after_load=lambda p: None,
        explain_load_failure=lambda exc: None,
    )
    with pytest.raises(FileNotFoundError):
        _lora.apply_stack(
            pipe,
            profile,
            entries=[
                _lora.ResolvedEntry(
                    ref="r:a", path=files["a.safetensors"], strength=1.0, target=None
                ),
                _lora.ResolvedEntry(
                    ref="r:b", path=files["b.safetensors"], strength=1.0, target=None
                ),
            ],
        )
    assert _lora.inventory_snapshot() == []
    assert pipe.unload_count == 2
    assert pipe.loaded == []


def test_same_ref_twice_reports_two_rows(files: dict[str, Path]) -> None:
    """Two adapters means two inventory rows, even when the ref repeats.

    Catches keying the inventory by (ref, target): both entries load and both
    reach set_adapters, so a one-row inventory under-reports what is active.
    """
    pipe = FakePipe()
    entry = _lora.ResolvedEntry(
        ref="r:a", path=files["a.safetensors"], strength=0.5, target=None
    )
    inv = _lora.apply_stack(pipe, _profile(pipe), entries=[entry, entry])
    assert [e.adapter_name for e in inv] == ["lora_0", "lora_1"]
    assert [e.ref for e in inv] == ["r:a", "r:a"]
    assert pipe.module.adapter_calls == [(["lora_0", "lora_1"], [0.5, 0.5])]
    assert len(_lora.inventory_snapshot()) == 2


@dataclass
class FakeSpec:
    """Duck-typed stand-in for the orchestrator's download spec."""

    url: str
    filename: str
    headers: dict[str, str] = field(default_factory=dict)


class _FakeResponse:
    """urlopen context manager yielding ``chunks`` then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


def test_download_one_streams_to_disk_with_the_civitai_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CivitAI 403s the default urllib UA, and a stall must not hang forever.

    Catches a port that drops the UA header or the 600 s timeout — both are
    silent until a live download either 403s or hangs the pod indefinitely.
    """
    seen: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResponse:
        seen["headers"] = dict(req.headers)
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return _FakeResponse([b"ab" * 8, b"cd"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    path, written = _lora.download_one(
        FakeSpec(url="https://vendor/x.safetensors", filename="x.safetensors"), tmp_path
    )

    assert isinstance(path, Path)
    assert path == tmp_path / "x.safetensors"
    assert path.read_bytes() == b"ab" * 8 + b"cd"
    assert written == 18
    assert seen["timeout"] == 600
    # urllib title-cases header keys on the Request.
    assert seen["headers"]["User-agent"] == "kinoforge-pod-download/0.1"
    assert not (tmp_path / "x.safetensors.partial").exists()


def test_download_one_leaves_no_partial_behind_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncated download must never present as a complete LoRA file.

    Catches dropping the ``.partial`` cleanup: the next boot would load a
    half-written safetensors and fail deep inside diffusers, or worse, load it.
    """

    class _Exploding(_FakeResponse):
        def read(self, _size: int) -> bytes:
            raise OSError("connection reset")

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _Exploding([])
    )
    with pytest.raises(OSError, match="connection reset"):
        _lora.download_one(
            FakeSpec(url="https://vendor/x.safetensors", filename="x.safetensors"),
            tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


def test_ensure_downloaded_reuses_the_file_already_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm pod must not re-pay for bytes it already holds.

    Catches an unconditional download, which on a warm-reuse pod re-fetches
    every LoRA on every set_stack call.
    """

    def explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("ensure_downloaded must not hit the network")

    spec = FakeSpec(url="https://vendor/x.safetensors", filename="x.safetensors")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse([b"z" * 42])
    )
    first, _ = _lora.ensure_downloaded("r:x", spec, tmp_path)
    assert first.read_bytes() == b"z" * 42

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    again, size = _lora.ensure_downloaded("r:x", spec, tmp_path)
    assert again == first
    assert size == 42


def test_ensure_downloaded_does_not_serve_another_refs_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vendor basenames collide; the ref is the identity, so bytes are per ref.

    ``pytorch_lora_weights.safetensors`` is a common CivitAI/HF basename. Reuse
    keyed on the filename alone would hand ref B whatever ref A downloaded
    first — the wrong LoRA, silently, for the life of a warm pod.
    """
    name = "pytorch_lora_weights.safetensors"

    def serve(payload: bytes) -> Callable[..., _FakeResponse]:
        return lambda req, timeout=None: _FakeResponse([payload])

    monkeypatch.setattr(urllib.request, "urlopen", serve(b"AAAA"))
    path_a, _ = _lora.ensure_downloaded(
        "civitai:1@1", FakeSpec(url="https://vendor/a", filename=name), tmp_path
    )
    monkeypatch.setattr(urllib.request, "urlopen", serve(b"BBBBBB"))
    path_b, size_b = _lora.ensure_downloaded(
        "civitai:2@2", FakeSpec(url="https://vendor/b", filename=name), tmp_path
    )

    assert path_a != path_b
    assert path_a.read_bytes() == b"AAAA"
    assert path_b.read_bytes() == b"BBBBBB"
    assert size_b == 6


@pytest.mark.parametrize(
    "ref",
    ["../../etc/passwd", "hf:org/repo/file.safetensors", "..", "/abs/path", "."],
)
def test_ref_directories_stay_inside_the_lora_dir(ref: str, tmp_path: Path) -> None:
    """A ref is cfg-supplied data; it must never escape the LoRA directory.

    Catches interpolating the ref into a path unsanitized, which for
    ``../../etc/passwd`` would write outside the LoRA directory entirely.
    """
    resolved = (tmp_path / _lora._ref_dirname(ref)).resolve()
    assert resolved.parent == tmp_path.resolve()


def test_ref_directories_do_not_collide_after_sanitizing() -> None:
    """Two refs that sanitize to the same text must still get separate dirs.

    ``hf:a/b`` and ``hf:a_b`` both flatten to ``hf_a_b``; without the digest
    suffix that reintroduces exactly the wrong-bytes bug the split prevents.
    """
    assert _lora._ref_dirname("hf:a/b") != _lora._ref_dirname("hf:a_b")
