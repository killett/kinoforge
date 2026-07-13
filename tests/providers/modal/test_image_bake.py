"""Behavior: ModalProvider bakes build_script into the image; boots runtime-only.

The 2026-07-09 FlashVSR live run died because Modal provisioned the heavy deps
(pip torch, 526 MB BSA wheel, FlashVSR weights) at container START — a ~15 min
window Modal kept preempting. Baking those into the image via run_commands makes
container start seconds; the boot payload must then carry ONLY the runtime
script so nothing heavy re-runs (and re-opens that window).
"""

from __future__ import annotations

import base64
import gzip
from typing import Any

from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.modal._app import ModalAppRequest, build_modal_app

_Calls = list[tuple[str, str]]


# --- fakes for build_modal_app -------------------------------------------


class _FakeImage:
    def __init__(self, calls: _Calls, tag: str) -> None:
        self.calls = calls
        self.tag = tag

    def run_commands(self, cmd: str) -> _FakeImage:
        self.calls.append(("run_commands", cmd))
        return self  # chainable, mirrors Modal's Image API

    def apt_install(self, *pkgs: str) -> _FakeImage:
        for p in pkgs:
            self.calls.append(("apt_install", p))
        return self


class _FakeWebServer:
    def __call__(
        self, port: int, *, startup_timeout: float = 5.0, label: str | None = None
    ) -> Any:
        return lambda fn: fn


class _FakeApp:
    def __init__(self, name: str, image: object) -> None:
        self.name = name
        self.image = image

    def function(self, **kwargs: object) -> Any:
        return lambda fn: fn


class _FakeModal:
    def __init__(self, calls: _Calls) -> None:
        self.calls = calls
        self.web_server = _FakeWebServer()
        outer = self

        class Image:
            @staticmethod
            def from_registry(tag: str, add_python: str | None = None) -> _FakeImage:
                outer.calls.append(("from_registry", tag))
                return _FakeImage(outer.calls, tag)

        class Volume:
            @staticmethod
            def from_name(name: str, create_if_missing: bool = False) -> str:
                return f"volume::{name}"

        class Secret:
            @staticmethod
            def from_dict(d: dict[str, str]) -> str:
                return "secret::obj"

        self.Image = Image
        self.Volume = Volume
        self.Secret = Secret

    def App(self, name: str, image: object) -> _FakeApp:  # noqa: N802 — mirror modal API
        self.app = _FakeApp(name, image)
        return self.app


def test_build_modal_app_bakes_build_script() -> None:
    # Bug caught: without run_commands(build_script), the image lacks torch/BSA/
    # weights and the container re-downloads at boot -> the ~15min preemption
    # window that killed the 2026-07-09 FlashVSR live run stays open.
    calls: _Calls = []
    fake_modal = _FakeModal(calls)
    req = ModalAppRequest(
        run_id="t",
        image="python:3.13-slim",
        gpu="A100-80GB",
        provision_script="exec server\n",
        run_cmd=["python", "-m", "s"],
        image_build_script="set -euo pipefail\npip install torch==2.6.0\n",
    )
    build_modal_app(req, fake_modal)
    bake_calls = [c for c in calls if c[0] == "run_commands"]
    assert len(bake_calls) == 1
    baked = bake_calls[0][1]
    # The bake RUN must be a SINGLE newline-free line: the Dockerfile parser
    # terminates a RUN at any bare newline BEFORE a shell sees it, so a raw
    # multi-line string ("the 'mkdir' Dockerfile command is not supported") and
    # a quoted `bash -c '<multi-line>'` ("Unterminated quoted string") both fail
    # (observed live 2026-07-10). Encode to one base64 blob + decode|bash.
    assert "\n" not in baked
    assert baked.startswith("echo ")
    assert "| base64 -d | bash" in baked
    # The blob must decode back to the real multi-line script.
    blob = baked[len("echo ") :].split(" | ", 1)[0]
    decoded = base64.b64decode(blob).decode()
    assert "pip install torch==2.6.0" in decoded
    assert decoded == req.image_build_script
    # from_registry must precede the bake (bake runs ON the base image).
    assert calls.index(("from_registry", "python:3.13-slim")) < calls.index(
        bake_calls[0]
    )
    # Slim image lacks curl/git/toolchain; all must be apt-installed before the
    # bake (curl: wheel+weights; git: FlashVSR git install; build-essential/
    # cmake/pkg-config: sentencepiece source build).
    for pkg in ("curl", "git", "build-essential", "cmake", "pkg-config"):
        assert ("apt_install", pkg) in calls, f"{pkg} not apt-installed"
        assert calls.index(("apt_install", pkg)) < calls.index(bake_calls[0])


def test_no_build_script_skips_run_commands() -> None:
    # Engines with no bakeable installs must not call run_commands at all.
    calls: _Calls = []
    req = ModalAppRequest(
        run_id="t",
        image="python:3.13-slim",
        gpu="A10",
        provision_script="exec server\n",
        run_cmd=["python", "-m", "s"],
        image_build_script=None,
    )
    build_modal_app(req, _FakeModal(calls))
    assert not [c for c in calls if c[0] == "run_commands"]


# --- create_instance boot-payload wiring ---------------------------------


def _spec(**over: Any) -> InstanceSpec:
    base: dict[str, Any] = dict(
        image="python:3.13-slim",
        offer=Offer(
            id="A100-80GB",
            gpu_type="A100-80GB",
            vram_gb=80,
            cuda="12.4",
            cost_rate_usd_per_hr=3.0,
        ),
        run_id="run1",
        run_cmd=["python", "-m", "s"],
        lifecycle=Lifecycle(boot_timeout_s=1800.0),
    )
    base.update(over)
    return InstanceSpec(**base)


def test_boot_payload_uses_runtime_script_only() -> None:
    # create_instance must pass the runtime script (no pip/BSA) as the boot
    # payload and forward the build script for the image bake.
    captured: dict[str, Any] = {}

    def _factory(req: ModalAppRequest, _mod: object) -> tuple[object, object]:
        captured["req"] = req
        return ("app", "server")

    provider = ModalProvider(
        app_factory=_factory,
        deployer=lambda a, s: "https://x.modal.run",
        clock=lambda: 0.0,
    )
    spec = _spec(
        provision_script="set -e\npip install torch\nexec server\n",
        runtime_provision_script="set -e\nexec server\n",
        image_build_script="set -e\npip install torch\n",
    )
    provider.create_instance(spec)
    req = captured["req"]
    assert "pip install" not in req.provision_script
    assert "exec server" in req.provision_script
    assert req.image_build_script and "pip install" in req.image_build_script


def test_boot_payload_falls_back_to_combined_when_no_runtime_split() -> None:
    # A non-splitting engine (runtime_provision_script None) still boots via the
    # combined provision_script — backward compatible.
    captured: dict[str, Any] = {}
    provider = ModalProvider(
        app_factory=lambda req, _m: (captured.setdefault("req", req), ("a", "s"))[1],
        deployer=lambda a, s: "https://x.modal.run",
        clock=lambda: 0.0,
    )
    provider.create_instance(_spec(provision_script="set -e\nexec server\n"))
    assert "exec server" in captured["req"].provision_script
    assert captured["req"].image_build_script is None


def test_baked_flashvsr_boot_payload_has_no_heavy_installs() -> None:
    # End-to-end: the real FlashVSR split, threaded onto a spec, yields a Modal
    # boot payload with the server exec but neither pip nor the BSA wheel.
    from kinoforge.core.config import load_config
    from kinoforge.engines.diffusers import DiffusersEngine

    rendered = DiffusersEngine().render_provision(
        load_config(
            "examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml"
        ).model_dump()
    )
    captured: dict[str, Any] = {}
    provider = ModalProvider(
        app_factory=lambda req, _m: (captured.setdefault("req", req), ("a", "s"))[1],
        deployer=lambda a, s: "https://x.modal.run",
        clock=lambda: 0.0,
    )
    spec = _spec(
        provision_script=rendered.script,
        runtime_provision_script=rendered.runtime_script,
        image_build_script=rendered.build_script,
    )
    provider.create_instance(spec)
    # The boot payload is provision_script + `exec run_cmd`.
    payload = captured["req"].provision_script
    assert "wan_t2v_server" in payload
    assert "pip install" not in payload
    assert "block_sparse_attn" not in payload
    # The build script (baked) carries them instead.
    assert "pip install" in captured["req"].image_build_script
    assert "block_sparse_attn" in captured["req"].image_build_script


def test_baked_payload_decodes_clean_in_secret_env() -> None:
    # The runtime boot payload must still gzip+base64 round-trip through the
    # Secret-chunk encoder (what the container reassembles).
    from kinoforge.providers.modal._app import _boot_payload, _payload_secret_env

    calls: _Calls = []
    fake_modal = _FakeModal(calls)
    req = ModalAppRequest(
        run_id="t",
        image="python:3.13-slim",
        gpu="A100-80GB",
        provision_script="set -e\nexec server\n",
        run_cmd=["python", "-m", "s"],
        image_build_script="pip install torch\n",
    )
    env = _payload_secret_env(_boot_payload(req))
    n = int(env["KINOFORGE_PROVISION_NCHUNKS"])
    blob = "".join(env[f"KINOFORGE_PROVISION_B64_{i}"] for i in range(n))
    decoded = gzip.decompress(base64.b64decode(blob)).decode()
    assert "exec server" in decoded
    assert "pip install" not in decoded
    build_modal_app(req, fake_modal)  # sanity: still builds with the bake
