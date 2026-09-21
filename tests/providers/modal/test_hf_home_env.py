"""Behavior: ModalProvider wires its pod directory layout onto the Volume mount.

The Modal Volume is mounted at ``spec.volume_mount`` (default ``/cache/hf``) but
without ``HF_HOME``, ``KINOFORGE_ARTIFACT_DIR``, and ``KINOFORGE_LORAS_DIR``
pointing there, the server either re-fetches HF weights from scratch on a
preempted/cold container or guesses at its own writable dirs. These tests pin
that ``create_instance`` seeds all three into the container env from the
resolved volume mount, while never clobbering an operator-supplied value.
"""

from kinoforge.core.interfaces import InstanceSpec, Launch, Lifecycle, SetupStep
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.modal._app import ModalAppRequest


def _spec(env: dict[str, str], volume_mount: str = "") -> InstanceSpec:
    """Build a minimal valid Modal InstanceSpec for an A100-80GB run."""
    return InstanceSpec(
        image="python:3.13-slim",
        run_id="run-hf",
        setup_steps=(SetupStep("echo provisioning"),),
        launch=Launch(("python", "-m", "server")),
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


def test_pod_dirs_default_to_the_volume_mount():
    # Bug caught: create_instance seeds HF_HOME but leaves the server to guess
    # its artifact/loras dirs, so a Modal container writes to /workspace/... —
    # RunPod's mount — landing on ephemeral container disk, not the Volume.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}))

    req = captured["req"]
    assert req.env["KINOFORGE_ARTIFACT_DIR"] == "/cache/hf/artifacts"
    assert req.env["KINOFORGE_LORAS_DIR"] == "/cache/hf/loras"


def test_pod_dirs_track_a_custom_volume_mount():
    # Bug caught: deriving the dirs from a hardcoded "/cache/hf" instead of the
    # RESOLVED mount desyncs them from where the Volume actually lives, the
    # same defect test_hf_home_tracks_a_custom_volume_mount pins for HF_HOME.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}, volume_mount="/mnt/weights"))

    req = captured["req"]
    assert req.env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/weights/artifacts"
    assert req.env["KINOFORGE_LORAS_DIR"] == "/mnt/weights/loras"


def test_pod_dirs_respect_operator_override():
    # Bug caught: plain assignment instead of setdefault would clobber a cfg
    # that deliberately redirects its artifact dir — a regression for anyone
    # already overriding it.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={"KINOFORGE_ARTIFACT_DIR": "/custom/art"}))

    req = captured["req"]
    assert req.env["KINOFORGE_ARTIFACT_DIR"] == "/custom/art"
    assert req.env["KINOFORGE_LORAS_DIR"] == "/cache/hf/loras", (
        "the key the caller did NOT set must still be derived"
    )


def test_hf_home_is_the_volume_root_never_a_subdir():
    # Bug caught: "unifying" Modal's layout with RunPod's, which puts HF_HOME at
    # <mount>/.hf_cache. Sub-project B's 144 GiB fetch lives at the Volume ROOT;
    # moving HF_HOME one level deeper orphans it and silently re-downloads
    # 123.8 GiB on the next run. This is the guard on that.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}))

    req = captured["req"]
    assert req.env["HF_HOME"] == "/cache/hf"
    assert not req.env["HF_HOME"].endswith(".hf_cache")
