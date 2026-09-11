"""Behavior: the spandrel weights fetch survives an unset ``HF_TOKEN`` (U32).

Provision scripts run under ``set -euo pipefail``. The spandrel upscaler is
the only place in kinoforge that interpolates ``${HF_TOKEN}`` directly into
shell — every other engine hands the token to a Python fetcher that reads it
with ``os.environ.get`` and tolerates absence — and it did so unguarded.

On RunPod that never showed: the env is present when the container starts and
the whole script runs there. On Modal the weights fetch is a BAKEABLE step, so
it runs at image-build time, where no run secrets exist — and ``set -u`` turns
the missing variable into ``HF_TOKEN: unbound variable``, exit 1, and a failed
image build. Observed live 2026-09-11 while standing up the U13 probe: two
Modal builds died there before any pod was created.

The test drives real ``bash`` with the variable genuinely unset rather than
asserting on the spelling of the line, so it fails for the reason the build
failed instead of for a formatting choice.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from kinoforge.upscalers.spandrel import SpandrelEngine

_CFG: dict[str, object] = {
    "upscale": {
        "engine": "spandrel",
        "scale": "2x",
        "spandrel": {
            "model_url": "hf:ai-forever/Real-ESRGAN/RealESRGAN_x2.pth",
            "arch": "realesrgan",
            "precision": "fp16",
            "tile_size": 512,
            "batch_size": 4,
        },
    }
}


def _auth_lines(script: str) -> list[str]:
    """Return the rendered lines that reference the token.

    Args:
        script: The rendered provision script.

    Returns:
        Every line mentioning ``HF_TOKEN``.
    """
    return [line for line in script.splitlines() if "HF_TOKEN" in line]


def test_weights_fetch_does_not_abort_a_set_u_script_without_a_token() -> None:
    """The fetch line must survive ``set -u`` with no ``HF_TOKEN`` in the env.

    Bug caught: ``-H "Authorization: Bearer ${HF_TOKEN}"`` with no default.
    Provision scripts run under ``set -euo pipefail``, and Modal bakes this
    step into the image, where run secrets do not exist — so the build dies
    with ``HF_TOKEN: unbound variable`` before a pod is ever created, which
    is exactly what two live Modal builds did on 2026-09-11.

    ``curl`` is shadowed by a no-op function so the test exercises the shell's
    expansion rules and nothing else: no network, and a failure here can only
    mean the script itself is unsafe.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present everywhere kinoforge runs
        pytest.skip("bash unavailable")

    script = SpandrelEngine().render_provision(_CFG).script
    lines = _auth_lines(script)
    assert lines, "no HF_TOKEN-bearing line rendered; the fixture stopped exercising it"

    probe = "set -euo pipefail\ncurl() { :; }\n" + "\n".join(lines) + "\n"
    env: dict[str, str] = {"PATH": "/usr/bin:/bin"}  # HF_TOKEN deliberately absent

    proc = subprocess.run(  # noqa: S603
        [bash, "-c", probe], capture_output=True, text=True, env=env, check=False
    )

    assert proc.returncode == 0, (
        f"the weights fetch aborts a set -u script when HF_TOKEN is unset — "
        f"this is the Modal image-build failure (U32). stderr={proc.stderr!r}"
    )


def test_a_present_token_is_still_sent() -> None:
    """Defaulting the variable must not stop the token being used.

    Bug caught: "fixing" the unbound-variable abort by dropping the
    Authorization header altogether. Public weights would still fetch, so
    every existing test and the RunPod path would stay green — and a GATED
    repo would start 401ing on a pod that is already billing.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash unavailable")

    script = SpandrelEngine().render_provision(_CFG).script
    lines = _auth_lines(script)

    # Echo the expanded command instead of running it, so the assertion is on
    # what curl would actually receive.
    probe = 'set -euo pipefail\ncurl() { echo "$@"; }\n' + "\n".join(lines) + "\n"
    proc = subprocess.run(  # noqa: S603
        [bash, "-c", probe],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HF_TOKEN": "hf-token-deadbeef"},
        check=False,
    )

    assert proc.returncode == 0
    assert "Bearer hf-token-deadbeef" in proc.stdout, (
        f"the token stopped reaching curl; got {proc.stdout!r}"
    )
