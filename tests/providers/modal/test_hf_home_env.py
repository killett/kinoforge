"""Behavior: ModalProvider wires HF_HOME onto the Volume mount for weight caching.

The Modal Volume is mounted at ``spec.volume_mount`` (default ``/cache/hf``) but
without ``HF_HOME`` pointing there, any HF download re-fetches from scratch on a
preempted/cold container. These tests pin that ``create_instance`` seeds
``HF_HOME`` = the resolved volume mount into the container env, while never
clobbering an operator-supplied value.
"""

from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.modal._app import ModalAppRequest


def _spec(env: dict[str, str], volume_mount: str = "") -> InstanceSpec:
    """Build a minimal valid Modal InstanceSpec for an A100-80GB run."""
    return InstanceSpec(
        image="python:3.13-slim",
        offer=Offer(
            id="A100-80GB",
            gpu_type="A100-80GB",
            vram_gb=80,
            cuda="12.4",
            cost_rate_usd_per_hr=2.10,
            mode="serverless",
        ),
        run_id="run-hf",
        provision_script="echo provisioning",
        run_cmd=["python", "-m", "server"],
        env=env,
        volume_mount=volume_mount,
        lifecycle=Lifecycle(idle_timeout_s=300),
    )


def _provider_capturing() -> tuple[ModalProvider, dict[str, ModalAppRequest]]:
    """A ModalProvider whose injected seams capture the ModalAppRequest offline."""
    captured: dict[str, ModalAppRequest] = {}

    def fake_factory(req, _modal_mod):
        captured["req"] = req
        return ("APP", "SERVERFN")

    def fake_deploy(_app, _server_fn):
        return "https://ws--kinoforge-run-hf-server.modal.run"

    provider = ModalProvider(app_factory=fake_factory, deployer=fake_deploy)
    return provider, captured


def test_hf_home_defaults_to_volume_mount():
    # Bug caught: create_instance builds env=dict(spec.env) without seeding
    # HF_HOME, so a preempted container re-downloads every weight instead of
    # reusing the Volume-mounted cache.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}))

    req = captured["req"]
    assert req.volume_mount == "/cache/hf"
    assert req.env["HF_HOME"] == "/cache/hf"


def test_hf_home_tracks_a_custom_volume_mount():
    # Bug caught: defaulting HF_HOME to a hardcoded "/cache/hf" instead of the
    # RESOLVED mount would desync the cache dir from where the Volume actually
    # lives when the operator overrides volume_mount.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}, volume_mount="/mnt/weights"))

    req = captured["req"]
    assert req.volume_mount == "/mnt/weights"
    assert req.env["HF_HOME"] == "/mnt/weights"


def test_hf_home_respects_operator_override():
    # Bug caught: a plain env["HF_HOME"] = volume_mount assignment would clobber
    # an operator-chosen cache dir; setdefault must leave it untouched.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={"HF_HOME": "/custom/cache"}))

    req = captured["req"]
    assert req.env["HF_HOME"] == "/custom/cache"
