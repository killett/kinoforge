"""Where a pod's writable directories live, derived from its volume mount.

``/workspace`` is RunPod's volume mount; Modal mounts its Volume at
``/cache/hf``; SkyPilot and Local attach no volume at all. A server module is
provider-agnostic and must therefore carry none of those literals as a runtime
default — the provider that knows its own mount exports these vars into the pod
env, and the server reads them.

``HF_HOME`` is deliberately NOT derived here. Modal's is the Volume root itself
and that Volume holds a 144 GiB fetch; RunPod's is a ``.hf_cache`` subdir on its
volume. That difference is real provider policy, so the caller passes it.

See docs/superpowers/specs/2026-09-21-pod-path-seam-and-ci-recovery-design.md.
"""

from __future__ import annotations

ARTIFACT_DIR_VAR = "KINOFORGE_ARTIFACT_DIR"
LORAS_DIR_VAR = "KINOFORGE_LORAS_DIR"
MODELS_DIR_VAR = "KINOFORGE_MODELS_DIR"
HF_HOME_VAR = "HF_HOME"

# Pod-local writable scratch for hosts with no attached volume. Mirrors the
# existing `_UPLOAD_DIR = Path("/tmp/kf-uploads")` convention in
# wan_t2v_server.py, including its justification: this is a pod's own tmpfs,
# not a shared multi-user /tmp.
SCRATCH_ARTIFACT_DIR = "/tmp/kf-artifacts"  # noqa: S108
SCRATCH_LORAS_DIR = "/tmp/kf-loras"  # noqa: S108
SCRATCH_MODELS_DIR = "/tmp/kf-models"  # noqa: S108


def pod_path_env(
    volume_mount: str | None, *, hf_home: str | None = None
) -> dict[str, str]:
    """Return the pod env vars naming the server's writable directories.

    Args:
        volume_mount: The provider's resolved volume mount path, or ``None`` /
            ``""`` when the host attaches no volume (SkyPilot, Local).
        hf_home: Absolute path for ``HF_HOME``, when the caller wants one set.
            Omitted from the result when ``None``, so ``huggingface_hub``'s own
            ``~/.cache/huggingface`` default applies. Never derived from
            ``volume_mount`` — see the module docstring.

    Returns:
        Mapping of env var name to absolute pod path, suitable for merging
        into a pod env dict with ``setdefault`` so an explicit config-supplied
        value still wins.
    """
    if volume_mount:
        root = volume_mount.rstrip("/")
        env = {
            ARTIFACT_DIR_VAR: f"{root}/artifacts",
            LORAS_DIR_VAR: f"{root}/loras",
            # U52. Upscaler/interpolator weight bundles (spandrel, FlashVSR,
            # SeedVR2, RIFE) — multi-GB fetches that belong on the volume,
            # not on container disk where Modal loses them every boot.
            MODELS_DIR_VAR: f"{root}/models",
        }
    else:
        env = {
            ARTIFACT_DIR_VAR: SCRATCH_ARTIFACT_DIR,
            LORAS_DIR_VAR: SCRATCH_LORAS_DIR,
            MODELS_DIR_VAR: SCRATCH_MODELS_DIR,
        }
    if hf_home is not None:
        env[HF_HOME_VAR] = hf_home
    return env
