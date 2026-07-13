"""Behavior: build_modal_app wires image/gpu/volume/secret/web_server correctly."""

import base64

import pytest

from kinoforge.providers.modal._app import ModalAppRequest, build_modal_app


class _FakeWebServer:
    def __init__(self):
        self.calls = []

    def __call__(self, port, *, startup_timeout=5.0, label=None):
        self.calls.append({"port": port, "startup_timeout": startup_timeout})
        return lambda fn: fn  # passthrough decorator


class _FakeApp:
    def __init__(self, name, image):
        self.name = name
        self.image = image
        self.function_kwargs = None

    def function(self, **kwargs):
        self.function_kwargs = kwargs
        return lambda fn: fn  # passthrough decorator


class _FakeModal:
    _last: "_FakeModal"  # set by the fixture; nested static methods record onto it

    def __init__(self):
        self.web_server = _FakeWebServer()
        self.from_registry_args = None
        self.volume_args = None
        self.secret_dict = None
        self.App = self._make_app

    def _make_app(self, name, image):
        self.app = _FakeApp(name, image)
        return self.app

    class Image:
        _outer = None

        @staticmethod
        def from_registry(tag, add_python=None):
            _FakeModal._last.from_registry_args = {"tag": tag, "add_python": add_python}
            return f"image::{tag}"

    class Volume:
        @staticmethod
        def from_name(name, create_if_missing=False):
            _FakeModal._last.volume_args = {
                "name": name,
                "create_if_missing": create_if_missing,
            }
            return f"volume::{name}"

    class Secret:
        @staticmethod
        def from_dict(d):
            _FakeModal._last.secret_dict = d
            return "secret::obj"


@pytest.fixture
def fake_modal():
    m = _FakeModal()
    _FakeModal._last = m  # let nested static methods record onto the instance
    return m


def _req():
    return ModalAppRequest(
        run_id="run123",
        image="runpod/pytorch:2.4.0-cuda12.4",
        gpu="A10",
        provision_script="echo provisioning; pip install foo",
        run_cmd=["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"],
        env={"HF_HOME": "/cache/hf"},
        volume_mount="/cache/hf",
        scaledown_window_s=300,
        startup_timeout_s=1800,
    )


def test_build_wires_image_app_volume(fake_modal):
    build_modal_app(_req(), fake_modal)
    assert fake_modal.from_registry_args == {
        "tag": "runpod/pytorch:2.4.0-cuda12.4",
        "add_python": None,  # image already ships Python; forcing add_python fails
    }
    assert fake_modal.app.name == "kinoforge-run123"
    assert fake_modal.volume_args == {
        "name": "kinoforge-hf-cache",
        "create_if_missing": True,
    }


def test_function_kwargs_carry_gpu_serialized_scaledown_volume(fake_modal):
    build_modal_app(_req(), fake_modal)
    kw = fake_modal.app.function_kwargs
    assert kw["gpu"] == "A10"
    assert kw["serialized"] is True  # cloudpickle the runtime-built fn
    assert kw["scaledown_window"] == 300
    assert kw["volumes"] == {
        "/cache/hf": "volume::A10".replace("A10", "kinoforge-hf-cache")
    }
    assert kw["secrets"] == ["secret::obj"]


def test_web_server_port_and_timeout(fake_modal):
    build_modal_app(_req(), fake_modal)
    assert fake_modal.web_server.calls == [{"port": 8000, "startup_timeout": 1800}]


def test_function_startup_timeout_governs_container_init(fake_modal):
    # Bug caught (Modal Milestone 2, 2026-07-08): with serialized=True, Modal
    # DROPS the @web_server(startup_timeout=...) and falls back to the FUNCTION's
    # startup_timeout, which defaults to `timeout` (300s). A ~63GB Wan 2.2 A14B
    # weight download takes ~30min → the container was killed at exactly 300s
    # ("Runner has been initializing for too long: 300 seconds"). The container
    # init window is the @app.function startup_timeout/timeout, so both MUST be
    # set from req.startup_timeout_s or any model whose boot exceeds 300s dies.
    build_modal_app(_req(), fake_modal)
    kw = fake_modal.app.function_kwargs
    assert kw["startup_timeout"] == 1800  # higher precedence than timeout
    assert kw["timeout"] == 1800  # startup_timeout defaults to timeout if unset


def test_function_pins_single_container(fake_modal):
    """Bug caught (live, 2026-07-12 EM1): without max_containers=1 Modal
    autoscales a second web-server container when polls queue behind a
    blocking request; job state + artifacts are per-container, so requests
    round-robin into intermittent 404s and the unretried artifact GET dies
    (status alternated 200/404; GET /artifacts -> 404; exit 1)."""
    build_modal_app(_req(), fake_modal)
    kw = fake_modal.app.function_kwargs
    assert kw["max_containers"] == 1


def test_secret_payload_contains_provision_and_run_cmd(fake_modal):
    # Bug caught: dropping run_cmd (server never launches) or the provision
    # script (deps/weights never installed) → dead container at startup.
    import gzip

    build_modal_app(_req(), fake_modal)
    n = int(fake_modal.secret_dict["KINOFORGE_PROVISION_NCHUNKS"])
    blob = "".join(
        fake_modal.secret_dict[f"KINOFORGE_PROVISION_B64_{i}"] for i in range(n)
    )
    # Every chunk must stay under Modal's 32768-byte per-value Secret cap.
    assert all(
        len(fake_modal.secret_dict[f"KINOFORGE_PROVISION_B64_{i}"]) <= 32768
        for i in range(n)
    )
    decoded = gzip.decompress(base64.b64decode(blob)).decode()
    assert "echo provisioning" in decoded
    assert "wan_t2v_server" in decoded  # run_cmd exec'd
    assert fake_modal.secret_dict["HF_HOME"] == "/cache/hf"  # env passed through
