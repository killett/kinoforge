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
        "add_python": "3.11",
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


def test_secret_payload_contains_provision_and_run_cmd(fake_modal):
    # Bug caught: dropping run_cmd (server never launches) or the provision
    # script (deps/weights never installed) → dead container at startup.
    build_modal_app(_req(), fake_modal)
    payload_b64 = fake_modal.secret_dict["KINOFORGE_PROVISION_B64"]
    decoded = base64.b64decode(payload_b64).decode()
    assert "echo provisioning" in decoded
    assert "wan_t2v_server" in decoded  # run_cmd exec'd
    assert fake_modal.secret_dict["HF_HOME"] == "/cache/hf"  # env passed through
