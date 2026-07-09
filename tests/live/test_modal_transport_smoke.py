"""LIVE: prove Modal deploy → live URL → teardown with a trivial HTTP server.

Runs only under `pixi run -e live-modal`. Marked `live` so the default suite skips.
"""

import time
import urllib.request

import pytest

pytestmark = pytest.mark.live


def test_modal_transport_end_to_end():
    from kinoforge.core.dotenv_loader import load_env_file
    from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer
    from kinoforge.providers.modal import ModalProvider

    load_env_file()
    provider = ModalProvider()
    # Trivial server: Python stdlib http.server on 0.0.0.0:8000, no provisioning.
    spec = InstanceSpec(
        image="python:3.11-slim",
        offer=Offer("T4", "T4", 16, "12.4", 0.59, mode="serverless"),
        run_id=f"smoke{int(time.time())}",
        provision_script="echo 'no provisioning needed'",
        run_cmd=["python", "-m", "http.server", "8000", "--bind", "0.0.0.0"],
        env={},
        lifecycle=Lifecycle(idle_timeout_s=60, boot_timeout_s=300),
    )
    inst = provider.create_instance(spec)
    try:
        url = inst.endpoints["8000"]
        assert url.startswith("https://") and url.endswith(".modal.run")
        # Poll until the server answers (bounded).
        deadline = time.time() + 300
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                    last = resp.status
                    if last == 200:
                        break
            except Exception as exc:  # noqa: BLE001
                last = repr(exc)
            time.sleep(5)
        assert last == 200, f"server never returned 200; last={last}"
    finally:
        provider.destroy_instance(inst.id)

    # Confirm teardown.
    names = {r.get("name") for r in provider._lister()}
    assert f"kinoforge-{spec.run_id}" not in names
