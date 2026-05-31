"""Tests for ComfyUIEngine + ComfyUIBackend (Task 20a).

All I/O seams (subprocess, HTTP, filesystem, sleep) are injected spies.
No real git, network, or ComfyUI traffic occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import FrameExtractionError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationJob,
    Instance,
    ModelProfile,
    Segment,
)
from kinoforge.engines.comfyui import ComfyUIEngine

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_DEFAULT_PROBE = ModelProfile(
    name="comfyui-test",
    max_frames=24,
    fps=8,
    supported_modes={"t2v"},
    max_resolution=(1024, 576),
    supports_native_extension=False,
    supports_joint_audio=False,
)

_INSTANCE = Instance(
    id="inst-1",
    provider="local",
    status="ready",
    created_at=0.0,
    endpoints={"comfyui": "http://localhost:8188"},
)


def _make_cfg(
    *,
    custom_nodes: list[dict[str, Any]] | None = None,
    launch_args: list[str] | None = None,
    models: list[dict[str, Any]] | None = None,
    probe: dict[str, Any] | None = None,
    flags_table: dict[str, dict[str, bool]] | None = None,
) -> dict[str, Any]:
    """Build a minimal config dict for ComfyUIEngine tests."""
    return {
        "engine": {
            "comfyui": {
                "custom_nodes": custom_nodes or [],
                "launch_args": launch_args or [],
                "probe": probe or {},
                "flags_table": flags_table or {},
            }
        },
        "models": models or [],
    }


def _make_engine(**kwargs: Any) -> ComfyUIEngine:
    """Return a ComfyUIEngine with all I/O seams replaced by safe no-ops."""
    defaults: dict[str, Any] = {
        "run_cmd": lambda argv, cwd=None: None,
        "file_exists": lambda p: False,
        "route_file": lambda src, dst_dir: None,
        "http_post": lambda url, body: {},
        "http_get": lambda url: {},
        "http_get_bytes": lambda url: b"",
        "ffmpeg_run": lambda argv, stdin: b"",
        "sleep": lambda s: None,
        "probe_profile": _DEFAULT_PROBE,
    }
    defaults.update(kwargs)
    return ComfyUIEngine(**defaults)


# ---------------------------------------------------------------------------
# AC1: provision clones each custom_nodes[].git via run_cmd spy
# ---------------------------------------------------------------------------


class TestProvisionClonesNodes:
    """AC1 — git clone commands issued for each custom_node entry."""

    def test_clones_each_node_in_order(self) -> None:
        """Assert run_cmd receives git clone for every node URL in order."""
        calls: list[list[str]] = []

        def spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=spy)
        cfg = _make_cfg(
            custom_nodes=[
                {"git": "https://github.com/org/node-a"},
                {"git": "https://github.com/org/node-b"},
            ],
            launch_args=[],
        )
        engine.provision(_INSTANCE, cfg)

        clone_calls = [c for c in calls if c[:2] == ["git", "clone"]]
        assert len(clone_calls) == 2
        assert "https://github.com/org/node-a" in clone_calls[0]
        assert "https://github.com/org/node-b" in clone_calls[1]

    def test_no_clone_when_no_custom_nodes(self) -> None:
        """No git calls when custom_nodes is empty."""
        calls: list[list[str]] = []

        def spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=spy)
        engine.provision(_INSTANCE, _make_cfg())

        clone_calls = [c for c in calls if c[:2] == ["git", "clone"]]
        assert clone_calls == []


# ---------------------------------------------------------------------------
# AC2: provision installs requirements.txt when file_exists says it does
# ---------------------------------------------------------------------------


class TestProvisionInstallsRequirements:
    """AC2 — pip install called for each node that has a requirements.txt."""

    def test_pip_install_called_when_requirements_present(self) -> None:
        """Assert pip install -r is called for nodes whose requirements exist."""

        # file_exists returns True only for paths containing "node-a"
        def file_exists_node_a(path: str) -> bool:
            return "node-a" in path

        calls: list[list[str]] = []

        def run_spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        cfg = _make_cfg(
            custom_nodes=[
                {"git": "https://github.com/org/node-a"},
                {"git": "https://github.com/org/node-b"},
            ]
        )
        engine = _make_engine(run_cmd=run_spy, file_exists=file_exists_node_a)
        engine.provision(_INSTANCE, cfg)

        pip_calls = [c for c in calls if c[:3] == ["pip", "install", "-r"]]
        assert len(pip_calls) == 1
        assert "node-a" in pip_calls[0][3]

    def test_no_pip_install_when_requirements_absent(self) -> None:
        """Assert pip install is NOT called when no node has requirements."""
        calls: list[list[str]] = []

        def run_spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=run_spy, file_exists=lambda p: False)
        engine.provision(
            _INSTANCE,
            _make_cfg(custom_nodes=[{"git": "https://github.com/org/node-z"}]),
        )

        pip_calls = [c for c in calls if len(c) >= 2 and c[0] == "pip"]
        assert pip_calls == []


# ---------------------------------------------------------------------------
# AC3: provision routes model files to the right ComfyUI subdir
# ---------------------------------------------------------------------------


class TestProvisionRoutesModels:
    """AC3 — route_file spy receives (src, dst_dir) with correct subdir."""

    def test_lora_routed_to_models_loras(self) -> None:
        """entry.target=='loras' → dst_dir ends with models/loras."""
        routes: list[tuple[str, str]] = []

        def route_spy(src: str, dst_dir: str) -> None:
            routes.append((src, dst_dir))

        engine = _make_engine(route_file=route_spy)
        cfg = _make_cfg(models=[{"src": "/tmp/my.safetensors", "target": "loras"}])
        engine.provision(_INSTANCE, cfg)

        assert len(routes) == 1
        assert "models/loras" in routes[0][1]

    def test_diffusion_models_routed_correctly(self) -> None:
        """entry.target=='diffusion_models' → dst_dir ends with models/diffusion_models."""
        routes: list[tuple[str, str]] = []

        def route_spy(src: str, dst_dir: str) -> None:
            routes.append((src, dst_dir))

        engine = _make_engine(route_file=route_spy)
        cfg = _make_cfg(
            models=[{"src": "/tmp/model.safetensors", "target": "diffusion_models"}]
        )
        engine.provision(_INSTANCE, cfg)

        assert len(routes) == 1
        assert "models/diffusion_models" in routes[0][1]

    def test_multiple_models_routed_in_order(self) -> None:
        """All model entries are routed, each to its correct subdir."""
        routes: list[tuple[str, str]] = []

        def route_spy(src: str, dst_dir: str) -> None:
            routes.append((src, dst_dir))

        engine = _make_engine(route_file=route_spy)
        cfg = _make_cfg(
            models=[
                {"src": "/tmp/a.safetensors", "target": "loras"},
                {"src": "/tmp/b.safetensors", "target": "vae"},
                {"src": "/tmp/c.safetensors", "target": "checkpoints"},
            ]
        )
        engine.provision(_INSTANCE, cfg)

        assert len(routes) == 3
        assert "models/loras" in routes[0][1]
        assert "models/vae" in routes[1][1]
        assert "models/checkpoints" in routes[2][1]


# ---------------------------------------------------------------------------
# AC4: provision launches ComfyUI with launch_args
# ---------------------------------------------------------------------------


class TestProvisionLaunchesComfyUI:
    """AC4 — run_cmd receives a launch command containing all launch_args."""

    def test_launch_command_contains_launch_args(self) -> None:
        """Assert run_cmd was called with main.py + explicit launch_args."""
        calls: list[list[str]] = []

        def run_spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=run_spy)
        cfg = _make_cfg(launch_args=["--listen", "0.0.0.0", "--port", "8188"])
        engine.provision(_INSTANCE, cfg)

        # Find the launch call (contains main.py)
        launch_calls = [c for c in calls if "main.py" in c]
        assert len(launch_calls) == 1
        launch_cmd = launch_calls[0]
        assert "--listen" in launch_cmd
        assert "0.0.0.0" in launch_cmd
        assert "--port" in launch_cmd
        assert "8188" in launch_cmd

    def test_launch_with_empty_args(self) -> None:
        """Provision still calls launch even when launch_args is empty."""
        calls: list[list[str]] = []

        def run_spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=run_spy)
        engine.provision(_INSTANCE, _make_cfg(launch_args=[]))

        launch_calls = [c for c in calls if "main.py" in c]
        assert len(launch_calls) == 1


# ---------------------------------------------------------------------------
# AC5: backend.submit POSTs graph+overrides and returns prompt_id
# ---------------------------------------------------------------------------


class TestBackendSubmit:
    """AC5 — submit merges graph with node_overrides and POSTs to /prompt."""

    def test_submit_posts_to_prompt_endpoint(self) -> None:
        """Assert http_post is called with the /prompt URL."""
        post_calls: list[tuple[str, Any]] = []

        def post_spy(url: str, body: Any) -> dict[str, Any]:
            post_calls.append((url, body))
            return {"prompt_id": "p-123"}

        engine = _make_engine(http_post=post_spy)
        backend = engine.backend(_INSTANCE, _make_cfg())
        job = GenerationJob(
            spec={
                "graph": {"6": {"inputs": {"text": "a cat"}}},
                "node_overrides": {"6": {"inputs": {"text": "a dog"}}},
            },
            segments=[Segment(prompt="test")],
        )
        prompt_id = backend.submit(job)

        assert prompt_id == "p-123"
        assert len(post_calls) == 1
        url, body = post_calls[0]
        assert "/prompt" in url

    def test_submit_overlays_node_overrides_onto_graph(self) -> None:
        """The posted body contains the graph with node_overrides applied."""
        posted_bodies: list[Any] = []

        def post_spy(url: str, body: Any) -> dict[str, Any]:
            posted_bodies.append(body)
            return {"prompt_id": "p-456"}

        engine = _make_engine(http_post=post_spy)
        backend = engine.backend(_INSTANCE, _make_cfg())
        job = GenerationJob(
            spec={
                "graph": {
                    "6": {"inputs": {"text": "original", "width": 512}},
                    "7": {"inputs": {"steps": 20}},
                },
                "node_overrides": {
                    "6": {"inputs": {"text": "overridden"}},
                },
            },
            segments=[Segment(prompt="test")],
        )
        backend.submit(job)

        assert len(posted_bodies) == 1
        body = posted_bodies[0]
        # node 6 text is overridden
        assert body["prompt"]["6"]["inputs"]["text"] == "overridden"
        # node 6 width preserved
        assert body["prompt"]["6"]["inputs"]["width"] == 512
        # node 7 untouched
        assert body["prompt"]["7"]["inputs"]["steps"] == 20


# ---------------------------------------------------------------------------
# AC6: backend.result polls history endpoint and returns Artifact
# ---------------------------------------------------------------------------


class TestBackendResult:
    """AC6 — result polls /history/{id} until outputs present; returns Artifact."""

    def test_result_polls_until_completed(self) -> None:
        """result returns Artifact after polling through running → completed."""
        call_count = 0

        def get_spy(url: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"p-123": {"status": "running"}}
            return {
                "p-123": {
                    "status": "completed",
                    "outputs": {"node_9": {"files": [{"filename": "clip.mp4"}]}},
                }
            }

        engine = _make_engine(http_get=get_spy, sleep=lambda s: None)
        backend = engine.backend(_INSTANCE, _make_cfg())
        artifact = backend.result("p-123")

        assert isinstance(artifact, Artifact)
        assert artifact.filename == "clip.mp4"
        assert artifact.meta["prompt_id"] == "p-123"
        assert call_count == 2

    def test_result_url_contains_history_and_prompt_id(self) -> None:
        """http_get is called with a URL containing /history/<prompt_id>."""
        urls: list[str] = []

        def get_spy(url: str) -> dict[str, Any]:
            urls.append(url)
            return {
                "p-abc": {
                    "status": "completed",
                    "outputs": {"node_1": {"files": [{"filename": "out.mp4"}]}},
                }
            }

        engine = _make_engine(http_get=get_spy, sleep=lambda s: None)
        backend = engine.backend(_INSTANCE, _make_cfg())
        backend.result("p-abc")

        assert len(urls) >= 1
        assert "/history/p-abc" in urls[0]


# ---------------------------------------------------------------------------
# AC7: validate_spec raises on missing keys; passes when both present
# ---------------------------------------------------------------------------


class TestValidateSpec:
    """AC7 — empty spec raises ValidationError; complete spec passes silently."""

    def test_empty_spec_raises_validation_error(self) -> None:
        """validate_spec({}) raises ValidationError."""
        engine = _make_engine()
        job = GenerationJob(spec={}, segments=[])
        with pytest.raises(ValidationError):
            engine.validate_spec(job)

    def test_missing_graph_raises_validation_error(self) -> None:
        """validate_spec raises when 'graph' is absent."""
        engine = _make_engine()
        job = GenerationJob(spec={"node_overrides": {}}, segments=[])
        with pytest.raises(ValidationError):
            engine.validate_spec(job)

    def test_missing_node_overrides_raises_validation_error(self) -> None:
        """validate_spec raises when 'node_overrides' is absent."""
        engine = _make_engine()
        job = GenerationJob(spec={"graph": {}}, segments=[])
        with pytest.raises(ValidationError):
            engine.validate_spec(job)

    def test_complete_spec_passes_silently(self) -> None:
        """validate_spec does NOT raise when both required keys are present."""
        engine = _make_engine()
        job = GenerationJob(
            spec={"graph": {}, "node_overrides": {}},
            segments=[],
        )
        engine.validate_spec(job)  # must not raise


# ---------------------------------------------------------------------------
# AC8: declared_flags returns per-key map from config-supplied table
# ---------------------------------------------------------------------------


class TestDeclaredFlags:
    """AC8 — declared_flags returns matching flags dict or {} for unknown key."""

    def test_known_key_returns_flags(self) -> None:
        """declared_flags returns the registered flags for a known key."""
        key = CapabilityKey(base_model="hf:org/model", engine="comfyui")
        derived = key.derive()

        flags_table = {derived: {"supports_native_extension": True}}

        def _noop_run(argv: list[str], cwd: str | None = None) -> None:
            pass

        engine = ComfyUIEngine(
            run_cmd=_noop_run,
            file_exists=lambda p: False,
            route_file=lambda src, dst_dir: None,
            http_post=lambda url, body: {},
            http_get=lambda url: {},
            sleep=lambda s: None,
            probe_profile=_DEFAULT_PROBE,
            flags_table=flags_table,
        )
        result = engine.declared_flags(key)
        assert result == {"supports_native_extension": True}

    def test_unknown_key_returns_empty_dict(self) -> None:
        """declared_flags returns {} for a key not in the table."""
        engine = _make_engine()
        key = CapabilityKey(base_model="hf:nobody/unknown")
        assert engine.declared_flags(key) == {}


# ---------------------------------------------------------------------------
# AC9: self-registration — registry.get_engine("comfyui")() returns ComfyUIEngine
# ---------------------------------------------------------------------------


class TestSelfRegistration:
    """AC9 — module-level registration puts ComfyUIEngine into the registry."""

    def test_get_engine_returns_comfyui_engine(self) -> None:
        """registry.get_engine('comfyui')() returns a ComfyUIEngine instance."""
        factory = registry.get_engine("comfyui")
        instance = factory()
        assert isinstance(instance, ComfyUIEngine)

    def test_engine_name_is_comfyui(self) -> None:
        """ComfyUIEngine.name == 'comfyui'."""
        assert ComfyUIEngine.name == "comfyui"

    def test_requires_compute_true(self) -> None:
        """ComfyUIEngine.requires_compute is True."""
        assert ComfyUIEngine.requires_compute is True

    def test_requires_local_weights_true(self) -> None:
        """ComfyUIEngine.requires_local_weights is True."""
        assert ComfyUIEngine.requires_local_weights is True


# ---------------------------------------------------------------------------
# extract_last_frame + result() URL backfill (Layer extract_last_frame)
# ---------------------------------------------------------------------------


def test_result_populates_url_with_view_query() -> None:
    """ComfyUIBackend.result() backfills Artifact.url with /view?filename=...&type=output.

    Bug this catches: URL not set, or wrong query shape, leaving
    extract_last_frame unable to fetch the rendered bytes.
    """
    from kinoforge.engines.comfyui import ComfyUIBackend

    history_payload = {
        "PROMPT_ID": {
            "outputs": {
                "9": {"files": [{"filename": "clip.mp4"}]},
            }
        }
    }

    backend = ComfyUIBackend(
        http_post=lambda url, body: {"prompt_id": "PROMPT_ID"},
        http_get=lambda url: history_payload,
        base_url="http://localhost:8188",
        probe=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result("PROMPT_ID")

    assert artifact.filename == "clip.mp4"
    assert artifact.url == "http://localhost:8188/view?filename=clip.mp4&type=output"
    assert artifact.meta == {"prompt_id": "PROMPT_ID"}


def test_extract_last_frame_fetches_url_and_calls_ffmpeg() -> None:
    """extract_last_frame: http_get_bytes(artifact.url) -> ffmpeg_run -> return.

    Bug this catches: engine fetches the wrong URL (e.g. from meta), or
    skips ffmpeg, or drops the bytes returned by the decoder.
    """
    fetch_calls: list[str] = []
    ffmpeg_calls: list[tuple[list[str], bytes]] = []

    def fake_fetch(url: str) -> bytes:
        fetch_calls.append(url)
        return b"VIDEO_BYTES"

    def fake_ffmpeg(argv: list[str], stdin: bytes) -> bytes:
        ffmpeg_calls.append((argv, stdin))
        return b"PNG_BYTES"

    engine = _make_engine(http_get_bytes=fake_fetch, ffmpeg_run=fake_ffmpeg)

    artifact = Artifact(
        filename="clip.mp4",
        url="http://localhost:8188/view?filename=clip.mp4&type=output",
        meta={"prompt_id": "X"},
    )

    out = engine.extract_last_frame(artifact)

    assert out == b"PNG_BYTES"
    assert fetch_calls == ["http://localhost:8188/view?filename=clip.mp4&type=output"]
    assert len(ffmpeg_calls) == 1
    assert ffmpeg_calls[0][1] == b"VIDEO_BYTES"


def test_extract_last_frame_raises_on_empty_url() -> None:
    """artifact.url == '' is unrecoverable; raise FrameExtractionError with
    engine class name in the message.

    Bug this catches: engine swallows the bad input and hits ffmpeg with
    empty bytes (which produces a less actionable error).
    """
    engine = _make_engine()
    artifact = Artifact(filename="clip.mp4", url="", meta={})

    with pytest.raises(FrameExtractionError, match="ComfyUIEngine"):
        engine.extract_last_frame(artifact)


def test_urllib_get_bytes_default_is_callable() -> None:
    """The shipped default for http_get_bytes is a real callable, not None.

    Bug this catches: engine constructor accepts None for the seam, making
    extract_last_frame crash at call time on production paths.
    """
    from kinoforge.engines.comfyui import _urllib_get_bytes

    assert callable(_urllib_get_bytes)


def test_result_url_encodes_filename_with_special_chars() -> None:
    """ComfyUIBackend.result() percent-encodes the filename in the /view URL.

    Bug this catches: filenames with spaces, '&', '=', '+', or non-ASCII
    bytes are interpolated raw, producing malformed URLs that urlopen
    rejects or that silently fetch the wrong resource.
    """
    from kinoforge.engines.comfyui import ComfyUIBackend

    payload = {
        "PID": {
            "outputs": {
                "9": {"files": [{"filename": "clip frame&01.mp4"}]},
            }
        }
    }
    backend = ComfyUIBackend(
        http_post=lambda url, body: {"prompt_id": "PID"},
        http_get=lambda url: payload,
        base_url="http://localhost:8188",
        probe=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result("PID")

    # Filename preserved as-is on the Artifact.
    assert artifact.filename == "clip frame&01.mp4"
    # URL encodes the unsafe chars (space -> %20, & -> %26).
    assert (
        artifact.url
        == "http://localhost:8188/view?filename=clip%20frame%2601.mp4&type=output"
    )


def test_extract_last_frame_wraps_fetch_failure_as_frame_extraction_error() -> None:
    """HTTP fetch errors surface as FrameExtractionError with the URL in the
    message, not as raw urllib exceptions.

    Bug this catches: callers expecting the spec-promised single exception
    type (FrameExtractionError) get an unrelated network exception instead.
    """

    class _NetBlewUp(RuntimeError):
        pass

    def boom(url: str) -> bytes:
        raise _NetBlewUp("connection refused")

    engine = _make_engine(http_get_bytes=boom)
    artifact = Artifact(
        filename="clip.mp4",
        url="http://localhost:8188/view?filename=clip.mp4&type=output",
        meta={},
    )

    with pytest.raises(FrameExtractionError, match="fetch from"):
        engine.extract_last_frame(artifact)
