"""DiffusersEngine + DiffusersBackend — adapter for a locally-running diffusers inference server.

All I/O (subprocess, HTTP, sleep) is routed through injected callables so that
tests can spy without any real side-effects.

Module-level self-registration puts the factory in the global registry under
the ``"diffusers"`` key so that ``registry.get_engine("diffusers")()`` works.
"""

from __future__ import annotations

import base64
import gzip
import importlib.resources
import json
import logging
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kinoforge.core import frames, registry

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
from kinoforge.core.assets import find_asset, set_by_dot_path
from kinoforge.core.boot_liveness import BootVerdict
from kinoforge.core.errors import (
    FrameExtractionError,
    ProvisionFailed,
    ProvisionTimeout,
    ValidationError,
)
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
    RenderedProvision,
)
from kinoforge.core.lora import LoraEntry

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum poll iterations in :meth:`DiffusersBackend.result`. Each
#: iteration sleeps 1 s, so this is the total seconds the backend
#: will wait for a single job to complete. Bumped from 60 (which fit
#: small image-gen jobs but timed out Task 8 attempt #25's 14B Wan
#: video at ~30s into the run) to 1800 (30 min) — covers a worst-case
#: 81-frame, 480x480, 20-step Wan 2.2 generation on an A100 80GB
#: (~5-10 min nominal, ~25 min if the GPU is unexpectedly slow).
_MAX_POLL = 1800

#: Default server base URL.
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: Default container image for remote provisioning. The torch 2.4
#: image is the most widely-cached runpod/pytorch tag — Task 8
#: attempts #13-15 hit 3 consecutive image-pull stalls on the
#: torch 2.8 cudnn-devel tag (RunPod machines without that 9.5 GB
#: image cached), each wasting $0.06-0.17 of idle pod time. Torch
#: itself is pip-upgraded above the cfg deps via ``--extra-index-url``
#: so the in-pod runtime is torch 2.6+ regardless. See
#: ``_PYTORCH_EXTRA_INDEX_URL`` below.
_DEFAULT_RUNPOD_IMAGE: str = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

#: PyTorch wheel index — used by the bootstrap's pip install line so
#: ``torch>=2.6`` resolves against cu124 wheels (cuda 12.4 = the
#: runpod/pytorch:2.4 image's CUDA). ``--extra-index-url`` (not
#: ``--index-url``) keeps PyPI as the primary index so other deps
#: like ``diffusers`` still resolve.
_PYTORCH_EXTRA_INDEX_URL: str = "https://download.pytorch.org/whl/cu124"

#: Seconds between readiness polls in :meth:`DiffusersEngine.wait_for_ready`.
_READY_POLL_INTERVAL_S: float = 5.0

#: Throttle for the boot-liveness probe inside wait_for_ready — consulted at
#: most this often, not on every /health poll (2026-07-07).
_BOOT_PROBE_INTERVAL_S: float = 30.0

#: User-Agent for outbound HTTP from this engine. Cloudflare (RunPod's
#: proxy edge) returns 403 to the default ``Python-urllib/3.13`` UA —
#: Task 8 attempt #22 stranded wait_for_ready in a 403-swallowing
#: retry loop for ~12 minutes against a live, healthy pod. Sending a
#: plain ``kinoforge`` tag clears the gate.
_KINOFORGE_USER_AGENT: str = "kinoforge-diffusers/0.1"


def _render_embed_single_file(mod_name: str, kfsrv: str) -> list[str]:
    """Embed a single dotted module (leaf is a .py file, not a package).

    Used for `embed_files` cfg entries that need a specific module without
    dragging in its whole package — e.g. `kinoforge.core.errors` embeds
    errors.py while leaving the rest of kinoforge/core/ off the pod.
    """
    parts = mod_name.split(".")
    parent_pkg = ".".join(parts[:-1])
    leaf = parts[-1]
    parent_root = importlib.resources.files(parent_pkg)
    resource = parent_root / f"{leaf}.py"
    if not resource.is_file():
        raise ValueError(
            f"embed_files entry {mod_name!r}: parent={parent_pkg!r} has no "
            f"file {leaf}.py"
        )
    rel = mod_name.replace(".", "/") + ".py"
    target = f"{kfsrv}/{rel}"
    lines: list[str] = []
    # Touch ancestor __init__.py files (same shape as the package embed).
    for i in range(1, len(parts)):
        ancestor_dir = f"{kfsrv}/" + "/".join(parts[:i])
        lines.append(f"mkdir -p {ancestor_dir}")
        lines.append(f"touch {ancestor_dir}/__init__.py")
    lines.append(f"mkdir -p {Path(target).parent}")
    content = resource.read_bytes()
    # mtime=0: gzip stamps the current time into its header by default, so the
    # same module bytes produce a DIFFERENT base64 blob every render — the boot
    # script is non-deterministic run-to-run. Zeroing mtime makes render_provision
    # reproducible (required for the build/runtime split golden test) with no
    # change to what decompresses on the pod.
    encoded = base64.b64encode(gzip.compress(content, mtime=0)).decode("ascii")
    lines.append(
        f"echo '{encoded}' | python3 -c "
        f'"import sys,base64,gzip; '
        f"sys.stdout.buffer.write("
        f'gzip.decompress(base64.b64decode(sys.stdin.read())))" '
        f"> {target}"
    )
    return lines


def _render_embed_lines(modules: list[str]) -> list[str]:
    """Return bash lines that recreate ``modules``' package trees under /tmp/kfsrv/.

    For each dotted package name, walks the package's directory files,
    base64-encodes each .py file, and emits an ``echo '<b64>' | base64 -d
    > /tmp/kfsrv/<rel>`` line. Also ensures every ancestor namespace
    has an ``__init__.py`` (touched empty for parents whose source dir
    might not ship one). Designed so the pod can run
    ``python -m <module>`` without kinoforge installed via pip.

    Args:
        modules: List of dotted package names, e.g.
            ``["kinoforge.engines.diffusers.servers"]``.

    Returns:
        Ordered bash lines: mkdir + base64-write + touch __init__.py.
    """
    # /tmp/kfsrv runs only on the freshly-provisioned single-tenant pod
    # (selfterm-bounded lifetime), not on a shared multi-user host —
    # ruff S108 suppressed accordingly.
    kfsrv = "/tmp/kfsrv"  # noqa: S108
    lines: list[str] = [f"mkdir -p {kfsrv}"]
    written_dirs: set[str] = set()
    written_inits: set[str] = set()
    for mod_name in modules:
        # Touch __init__.py at every ancestor namespace level so
        # `python -m <mod_name>...` can resolve the chain.
        parts = mod_name.split(".")
        for i in range(1, len(parts) + 1):
            ancestor_dir = f"{kfsrv}/" + "/".join(parts[:i])
            if ancestor_dir not in written_dirs:
                lines.append(f"mkdir -p {ancestor_dir}")
                written_dirs.add(ancestor_dir)
            init_path = f"{ancestor_dir}/__init__.py"
            if init_path not in written_inits:
                lines.append(f"touch {init_path}")
                written_inits.add(init_path)

        # Walk the package and emit base64 writes for each .py file.
        # Sorted by name: iterdir() yields filesystem enumeration order,
        # which differs across machines and would make the rendered script
        # (and the golden byte-identity test) host-dependent.
        pkg_root = importlib.resources.files(mod_name)
        for resource in sorted(pkg_root.iterdir(), key=lambda r: r.name):
            if not resource.is_file() or not resource.name.endswith(".py"):
                continue
            rel = mod_name.replace(".", "/") + "/" + resource.name
            target = f"{kfsrv}/{rel}"
            content = resource.read_bytes()
            # gzip+base64 keeps the bootstrap script under RunPod's 64KB
            # env-var ceiling for big embedded modules like wan_t2v_server.py
            # (~67KB raw → ~16KB gz → ~22KB b64). Pre-gzip embeds blew the
            # KINOFORGE_PROVISION_SCRIPT env var past the limit and the
            # create-pod mutation 500ed.
            #
            # Decode via python3 (always present in runpod/pytorch images)
            # rather than `gunzip` — saves us hunting for a gzip binary on
            # minimal images. python -c "..." keeps the pipeline stateless.
            # mtime=0 zeroes gzip's header timestamp so identical module bytes
            # render to identical base64 every run (deterministic boot script).
            encoded = base64.b64encode(gzip.compress(content, mtime=0)).decode("ascii")
            lines.append(
                f"echo '{encoded}' | python3 -c "
                f'"import sys,base64,gzip; '
                f"sys.stdout.buffer.write("
                f'gzip.decompress(base64.b64decode(sys.stdin.read())))" '
                f"> {target}"
            )
    return lines


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _extract_port_from_base_url(base_url: str) -> str:
    """Return the port in ``http(s)://host:PORT/...``, default ``'8000'``.

    Args:
        base_url: A URL string such as ``"http://localhost:8000"``
            or ``"https://host:9999/path"``.

    Returns:
        The port as a string, or ``"8000"`` when absent or empty.
    """
    if not base_url:
        return "8000"
    m = re.search(r":(\d+)", base_url)
    return m.group(1) if m else "8000"


def _default_get_instance(_: str) -> Instance:
    """Stub seam — orchestrator must inject provider.get_instance for remote provision.

    Args:
        _: Instance ID (unused).

    Raises:
        NotImplementedError: Always — seam not wired.
    """
    raise NotImplementedError(
        "DiffusersEngine.get_instance seam not wired — "
        "orchestrator must inject provider.get_instance"
    )


# ---------------------------------------------------------------------------
# Real I/O helpers (default implementations)
# ---------------------------------------------------------------------------


def _subprocess_run(argv: list[str], cwd: str | None = None) -> None:
    """Run *argv* in a subprocess, raising ``subprocess.CalledProcessError`` on failure.

    Args:
        argv: Command and arguments.
        cwd: Working directory; ``None`` inherits the caller's cwd.
    """
    subprocess.run(argv, cwd=cwd, check=True)  # noqa: S603


def _urllib_post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST *body* as JSON to *url* and return the decoded response.

    Args:
        url: Endpoint URL.
        body: JSON-serialisable request body.

    Returns:
        Decoded JSON response as a Python dict.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            # Cloudflare (RunPod's proxy fronting) returns 403 to the
            # default ``Python-urllib/X.Y`` User-Agent — Task 8 attempt
            # #22 hung wait_for_ready in a 403-swallowing retry loop
            # for ~12 minutes against a live, healthy pod. Sending a
            # plain UA tag clears the gate.
            "User-Agent": _KINOFORGE_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


def _urllib_get_json(url: str) -> dict[str, Any]:
    """GET *url* and return the decoded JSON response.

    Args:
        url: Endpoint URL.

    Returns:
        Decoded JSON response as a Python dict.
    """
    # Same Cloudflare-403 dodge as _urllib_post_json — see comment there.
    req = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": _KINOFORGE_USER_AGENT}
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


def _urllib_get_bytes(url: str) -> bytes:
    """GET *url* and return the raw response body as bytes.

    Args:
        url: Endpoint URL.

    Returns:
        Response body as bytes.
    """
    # Same Cloudflare-403 dodge as _urllib_post_json — see comment there.
    req = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": _KINOFORGE_USER_AGENT}
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return bytes(resp.read())


# ---------------------------------------------------------------------------
# Default probe profile
# ---------------------------------------------------------------------------

_DEFAULT_PROBE = ModelProfile(
    name="diffusers",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class DiffusersBackend(GenerationBackend):
    """Live backend that communicates with a running diffusers inference server.

    All HTTP traffic goes through injected callables so tests can spy
    without starting a real server.

    Attributes:
        _http_post: Callable ``(url, body) -> dict`` for POST requests.
        _http_get: Callable ``(url) -> dict`` for GET requests.
        _base_url: Base URL of the diffusers server (no trailing slash).
        _probe: The ``ModelProfile`` returned by capability queries.
        _sleep: Injectable sleep function (default: ``time.sleep``).
    """

    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        base_url: str,
        probe_profile: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
        asset_paths: dict[str, str] | None = None,
        prompt_body_key: str | None = "prompt",
        poll_timeout_s: float = 1800.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the diffusers server, e.g.
                ``"http://127.0.0.1:8000"``.  No trailing slash.
            probe_profile: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
            asset_paths: Optional mapping from role name to dot-path in the
                request body where the matching asset's URI is written
                (e.g. ``{"init_image": "init_image_url"}``).  Roles absent
                from this map are not injected.  URL passthrough only —
                ``submit`` never fetches the asset bytes; the diffusers
                server fetches the URL.
            prompt_body_key: Top-level body key written from
                ``resolve_prompt(job)`` when no explicit ``spec["prompt"]``
                is provided. ``None`` / empty disables routing entirely.
            poll_timeout_s: Wall-clock cap on ``result()`` polling.
                Default ``1800.0`` matches the legacy
                ``_MAX_POLL * 1.0s`` effective bound.
            poll_interval_s: Sleep between successive ``/status`` polls.
                Default ``1.0`` matches today's hard-coded value.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe_profile
        self._sleep = sleep
        self._asset_paths: dict[str, str] = dict(asset_paths or {})
        self._prompt_body_key: str | None = prompt_body_key
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s

    def capabilities(self) -> ModelProfile:
        """Return the injected probe profile.

        Returns:
            The ``ModelProfile`` supplied at construction time.
        """
        return self._probe

    def inspect_capabilities(self) -> ModelProfile:
        """Return the injected probe profile unchanged.

        Returns:
            The ``ModelProfile`` supplied at construction time.
        """
        return self._probe

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str:
        """POST the job spec (with asset URIs injected) to ``/generate``.

        For each role declared in ``self._asset_paths``, look up the
        corresponding asset on ``job.segments[0]`` via
        :func:`~kinoforge.core.assets.find_asset` and write
        ``asset.ref.uri`` into a copy of the request body at the
        configured dot-path via
        :func:`~kinoforge.core.assets.set_by_dot_path`.  Roles absent
        from ``segments[0].assets`` are silently skipped.  ``job.spec``
        itself is never mutated.

        URL passthrough only — no asset bytes are fetched here; the
        diffusers server fetches the URI.

        Args:
            job: The ``GenerationJob`` whose ``spec`` is the request body.
            cancel_token: Accepted for :class:`GenerationBackend` ABC
                parity; ignored — diffusers submit is synchronous.

        Returns:
            The ``job_id`` string from the server response.
        """
        del cancel_token  # ABC parity; this backend completes via poll in result().
        from kinoforge.core.prompt_routing import (
            resolve_prompt,  # local — avoid circular at module load
        )

        body = dict(job.spec)
        if self._prompt_body_key:
            prompt = resolve_prompt(job)
            if prompt is not None:
                body.setdefault(self._prompt_body_key, prompt)
        for role, dot_path in self._asset_paths.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            set_by_dot_path(body, dot_path, asset.ref.uri)
        url = f"{self._base_url}/generate"
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY,
            retry_proxy_call,
        )

        response = retry_proxy_call(
            "diffusers.submit",
            url,
            lambda: self._http_post(url, body),
            self._sleep,
            RUNPOD_PROXY_POLICY,
        )
        return str(response["job_id"])

    def result(
        self,
        job_id: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact:
        """Poll ``/status/{job_id}`` until ``status == "done"``.

        Honors *cancel_token* both at the top of every iteration and
        across the inter-poll wait. Absorbs transient HTTPError codes
        and transport-class exceptions (URLError, OSError) via
        :func:`retry_proxy_call`. On wall-clock timeout, re-raises the
        last absorbed transient in preference to a bare TimeoutError so
        operators see the underlying proxy failure.

        Args:
            job_id: The job ID returned by a prior ``submit`` call.
            cancel_token: Cooperative cancellation token. ``None``
                (or default) means cancellation is not honored.

        Returns:
            An ``Artifact`` whose ``filename`` comes from the server
            response and whose ``meta`` contains ``{"job_id": job_id}``.
            The ``url`` is built from this backend's ``base_url`` so
            remote pods resolve through the RunPod proxy (the
            server-supplied ``localhost:8000`` URL is ignored).

        Raises:
            TimeoutError: Wall-clock or iteration-count exceeded with
                no sustained transient to surface.
            urllib.error.HTTPError: Re-raised when a sustained transient
                caused the timeout (preferred over TimeoutError).
            urllib.error.URLError | OSError: Same as above for
                transport-class transients.
            kinoforge.core.errors.Cancelled: ``cancel_token`` fired.
            GenerationError: Server reported ``status == "error"``.
        """
        from kinoforge.core.cancel import _NULL_TOKEN
        from kinoforge.core.errors import GenerationError
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY,
            interpoll_wait,
            retry_proxy_call,
        )

        token = cancel_token if cancel_token is not None else _NULL_TOKEN
        url = f"{self._base_url}/status/{job_id}"
        start = time.monotonic()
        last_transient: BaseException | None = None
        poll_idx = 0
        while True:
            token.raise_if_set()
            elapsed = time.monotonic() - start
            if poll_idx >= _MAX_POLL or elapsed > self._poll_timeout_s:
                if last_transient is not None:
                    raise last_transient
                raise TimeoutError(
                    f"diffusers poll timed out after {elapsed:.1f}s "
                    f"(job={job_id}, polls={poll_idx})"
                )
            try:
                data = retry_proxy_call(
                    "diffusers.result",
                    url,
                    lambda: self._http_get(url),
                    self._sleep,
                    RUNPOD_PROXY_POLICY,
                    cancel_token=cancel_token,
                )
            except urllib.error.HTTPError as exc:
                if exc.code in RUNPOD_PROXY_POLICY.transient_codes:
                    _log.warning(
                        "[diffusers.result] transient HTTPError exhausted "
                        "elapsed=%.1fs job=%s code=%d",
                        elapsed,
                        job_id,
                        exc.code,
                    )
                    last_transient = exc
                    poll_idx += 1
                    if interpoll_wait(self._poll_interval_s, cancel_token, self._sleep):
                        token.raise_if_set()
                    continue
                raise
            except RUNPOD_PROXY_POLICY.catch_classes as exc:
                _log.warning(
                    "[diffusers.result] transient transport-error exhausted "
                    "elapsed=%.1fs job=%s type=%s",
                    elapsed,
                    job_id,
                    type(exc).__name__,
                )
                last_transient = exc
                poll_idx += 1
                if interpoll_wait(self._poll_interval_s, cancel_token, self._sleep):
                    token.raise_if_set()
                continue

            status = data.get("status")
            if status == "done":
                filename = str(data.get("filename", ""))
                artifact_url = f"{self._base_url.rstrip('/')}/artifacts/{filename}"
                return Artifact(
                    filename=filename, url=artifact_url, meta={"job_id": job_id}
                )
            if status == "error":
                err_msg = str(
                    data.get("error", "<server reported error with no message>")
                )
                raise GenerationError(
                    f"diffusers server reported error for job {job_id!r}: {err_msg}"
                )
            poll_idx += 1
            if interpoll_wait(self._poll_interval_s, cancel_token, self._sleep):
                token.raise_if_set()

    def endpoints(self) -> dict[str, str]:
        """Return the diffusers server endpoint map.

        Returns:
            A dict with ``"generate"`` and ``"status"`` endpoint URLs.
        """
        return {
            "generate": f"{self._base_url}/generate",
            "status": f"{self._base_url}/status",
        }

    def set_lora_stack(
        self,
        *,
        pod_id: str,
        active_stack: list[LoraEntry],
        download_specs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """POST /lora/set_stack — declarative LoRA swap on the pod.

        Maps pod-side structured failure bodies to the LoraSwapError
        hierarchy so the matcher + orchestrator can branch on the exact
        failure mode without parsing free-form strings.

        Args:
            pod_id: Pod identifier; surfaced in every raised exception so
                operators get a copy-paste-able recovery command.
            active_stack: Ordered list of :class:`LoraEntry` (ref +
                strength). Each entry maps to one ``LoraTarget`` on the
                wire; ``strength`` reaches ``set_adapters(adapter_weights=)``
                server-side. P1 (2026-06-21) — see spec §6.3.
            download_specs: Map of ref → {url, headers, filename,
                size_hint?}; covers every ref in ``active_stack`` that
                is not already on the pod.

        Returns:
            The parsed response body: ``{inventory, free_bytes,
            swap_rejected}``. Successful HTTP 200 cases — including those
            where the pod added a ``swap_rejected: null`` field.

        Raises:
            LoraSwapVramOomError: HTTP 200 but body's ``swap_rejected``
                has ``reason="vram_oom"`` — pod rolled back, healthy.
            LoraSwapDownloadError: HTTP 502 with empty
                ``evict_completed`` — pod inventory unchanged.
            LoraSwapDegradedPodError: HTTP 502 with non-empty
                ``evict_completed`` — pod is in half-state.
            LoraSwapDiskFullError: HTTP 507.
            LoraSwapPodUnreachableError: Transport error past the proxy
                retry budget.
        """
        from kinoforge.core.errors import (
            LoraSwapPodUnreachableError,
            LoraSwapVramOomError,
        )

        url = f"{self._base_url}/lora/set_stack"
        # P1 (2026-06-21): wire shape is tagged objects
        # ``target: [{ref, strength}, ...]`` — pod-side migrator promotes
        # legacy ``target_refs`` to default strength=1.0 if present,
        # but no kinoforge.engines.diffusers.* call site ships the
        # legacy shape any more.
        # P2 (2026-06-22): wire shape carries ``branch`` per entry so the
        # pod-side ``LoraTarget`` routes per-LoRA to the right transformer.
        # ``LoraEntry.branch`` is always one of ``"high_noise"`` /
        # ``"low_noise"`` / ``"auto"`` (canonical form — h/l aliases are
        # already normalized at LoraEntry validation time).
        body: dict[str, Any] = {
            "target": [
                {"ref": e.ref, "strength": e.strength, "branch": e.branch}
                for e in active_stack
            ],
            "download_specs": download_specs,
        }
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY,
            retry_proxy_call,
        )

        try:
            resp = retry_proxy_call(
                "diffusers.lora.set_stack",
                url,
                lambda: self._http_post(url, body),
                self._sleep,
                RUNPOD_PROXY_POLICY,
            )
        except Exception as e:
            status = getattr(e, "status", None)
            body_attr = getattr(e, "body", None)
            if status is not None and isinstance(body_attr, dict):
                self._raise_lora_swap_error(int(status), body_attr, pod_id)
            raise LoraSwapPodUnreachableError(pod_id=pod_id, underlying=str(e)) from e

        sr = resp.get("swap_rejected") if isinstance(resp, dict) else None
        if isinstance(sr, dict) and sr.get("reason") == "vram_oom":
            raise LoraSwapVramOomError(
                pod_id=pod_id,
                dropped_refs=list(sr.get("target_refs_dropped", [])),
            )
        return resp

    def _raise_lora_swap_error(
        self, status: int, body: dict[str, Any], pod_id: str
    ) -> None:
        """Translate a /lora/set_stack failure body into the matching exception.

        Args:
            status: HTTP status code from the pod-side response.
            body: Structured error body from the pod.
            pod_id: Pod identifier for the raised exception.

        Raises:
            LoraSwapDiskFullError: For 507 or ``error="disk_full"``.
            LoraSwapDegradedPodError: For 502 + ``lora_download_failed`` with
                non-empty ``evict_completed``.
            LoraSwapDownloadError: For 502 + ``lora_download_failed`` with no
                eviction in progress.
            RuntimeError: For an unrecognised body shape.
        """
        from kinoforge.core.errors import (
            LoraSwapDegradedPodError,
            LoraSwapDiskFullError,
            LoraSwapDownloadError,
        )

        err = body.get("error")
        evict = list(body.get("evict_completed", []))
        failed = body.get("download_failed", "") or ""
        underlying = body.get("underlying", "") or ""
        if status == 507 or err == "disk_full":
            raise LoraSwapDiskFullError(
                pod_id=pod_id, evict_completed=evict, download_failed=failed
            )
        if status == 502 and err == "lora_download_failed":
            if evict:
                raise LoraSwapDegradedPodError(
                    pod_id=pod_id,
                    evict_completed=evict,
                    download_failed=failed,
                    underlying=underlying,
                )
            raise LoraSwapDownloadError(
                pod_id=pod_id, ref=failed, underlying=underlying
            )
        raise RuntimeError(f"unknown /lora/set_stack error body: {body}")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DiffusersEngine(GenerationEngine):
    """Generation engine adapter for a locally-provisioned diffusers inference server.

    Provisions the server by:

    1. Installing Python dependencies via ``pip install``.
    2. Launching the headless inference server with the configured command.

    All I/O is routed through injected callables (``run_cmd``, ``http_post``,
    ``http_get``, ``sleep``) so that tests can spy without real side-effects.

    Class attributes:
        name: Registry key ``"diffusers"``.
        requires_compute: ``True`` — a GPU instance is needed.
        requires_local_weights: ``True`` — weights must be provisioned locally.
    """

    name: str = "diffusers"
    requires_compute: bool = True
    requires_local_weights: bool = True

    def __init__(
        self,
        *,
        run_cmd: Callable[[list[str], str | None], None] = _subprocess_run,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] = _urllib_post_json,
        http_get: Callable[[str], dict[str, Any]] = _urllib_get_json,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
        sleep: Callable[[float], None] = time.sleep,
        probe_profile: ModelProfile = _DEFAULT_PROBE,
        declared_flags_map: dict[str, dict[str, bool]] | None = None,
        # NEW — Layer Q: remote-provisioning seam
        get_instance: Callable[[str], Instance] | None = None,
    ) -> None:
        """Initialise the engine with all I/O seams as injectable callables.

        Args:
            run_cmd: Callable ``(argv, cwd) -> None`` for subprocess calls.
            http_post: Callable ``(url, json_body) -> dict`` for HTTP POST.
            http_get: Callable ``(url) -> dict`` for HTTP GET.
            http_get_bytes: Callable ``(url) -> bytes`` for raw-byte HTTP GET
                (used by extract_last_frame to fetch the rendered video).
            ffmpeg_run: Subprocess seam ``(argv, stdin) -> stdout`` used by
                extract_last_frame to decode the last frame via ffmpeg.
            sleep: Sleep callable used between polling iterations in
                :meth:`~DiffusersBackend.result`.
            probe_profile: ``ModelProfile`` returned by backend capability
                queries.
            declared_flags_map: Optional mapping from
                :meth:`~kinoforge.core.interfaces.CapabilityKey.derive` hex
                strings to ``dict[str, bool]`` strategy-flag dicts.
            get_instance: Callable ``(instance_id) -> Instance`` for status
                checks during :meth:`wait_for_ready`. Injected by the
                orchestrator at runtime; defaults to a stub that raises
                ``NotImplementedError``.
        """
        self._run_cmd = run_cmd
        self._http_post = http_post
        self._http_get = http_get
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
        self._sleep = sleep
        self._probe = probe_profile
        self._declared_flags_map: dict[str, dict[str, bool]] = declared_flags_map or {}
        self._get_instance: Callable[[str], Instance] = (
            get_instance if get_instance is not None else _default_get_instance
        )
        # Asset-role -> request-body dot-path map, populated by ``backend``
        # from ``cfg["engine"]["diffusers"]["asset_paths"]`` and mirrored
        # onto the engine so ``validate_spec`` can check it.
        self._asset_paths: dict[str, str] = {}
        # Prompt-routing config: top-level body key mirrored from
        # ``cfg["engine"]["diffusers"]["prompt_body_key"]`` at backend()
        # time. ``None`` disables routing.
        self._prompt_body_key: str | None = "prompt"

    def refs_to_stage(self, merged: list[Artifact]) -> list[Artifact]:
        """Return the artifacts the provisioner should download to the workspace.

        The DiffusersEngine pod self-fetches model weights at server-start
        time via ``diffusers.WanPipeline.from_pretrained(MODEL_ID)`` (or
        the equivalent ``from_pretrained`` call in any other diffusers
        serving module). The workspace-side ``download_all`` path runs
        before ``render_provision`` ships the bootstrap script, and
        ``render_provision`` does NOT transfer those local bytes to the
        pod — meaning every byte downloaded locally is wasted.

        Returning ``[]`` skips the workspace download entirely. The pod's
        HF cache is the single source of truth for weights.

        See plan amendment 2026-06-19, Task 7.5 for the original finding.

        Args:
            merged: The merged artifact list the provisioner produced; ignored
                here because the engine never needs workspace-side staging.

        Returns:
            An empty list — no artifacts to stage locally.
        """
        del merged
        return []

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, Any],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """Install pip deps and launch the headless diffusers inference server (local), or wait for a remote pod to be ready.

        For local instances (``instance is None`` or
        ``instance.provider == "local"``), provision steps are:

        1. If ``cfg["engine"]["diffusers"]["pip"]`` is non-empty, run
           ``pip install`` with the declared package list.
        2. Launch the server with ``cfg["engine"]["diffusers"]["server_cmd"]``.

        For remote instances, the bootstrap script has already run via the
        provider's boot path; this method simply waits for the engine's
        HTTP ready-check to succeed.

        Args:
            instance: The compute instance; ``None`` implies local.
            cfg: Runtime configuration dict.
            cancel_token: C29 cooperative cancellation. Forwarded into the
                remote-path :meth:`wait_for_ready` call. ``None`` preserves
                pre-C29 behaviour.
        """
        if instance is None or instance.provider == "local":
            # Local body — unchanged.
            engine_block = cfg.get("engine", {})
            diffusers_cfg: dict[str, Any] = (
                engine_block.get("diffusers", {})
                if isinstance(engine_block, dict)
                else {}
            )
            pip_deps: list[str] = list(diffusers_cfg.get("pip", []))
            server_cmd: list[str] = list(diffusers_cfg.get("server_cmd", []))

            # Step 1 — install pip dependencies.
            if pip_deps:
                self._run_cmd(["pip", "install"] + pip_deps, None)

            # Step 2 — launch the headless inference server.
            if server_cmd:
                self._run_cmd(server_cmd, None)
            return

        # Remote branch: script already ran via provider boot path; just wait.
        lifecycle_block = cfg.get("lifecycle", {})
        boot_timeout_s = float(
            lifecycle_block.get("boot_timeout_s", 900.0)
            if isinstance(lifecycle_block, dict)
            else 900.0
        )
        self.wait_for_ready(
            instance,
            http_get=self._http_get,
            sleep=self._sleep,
            get_instance=self._get_instance,
            timeout_s=boot_timeout_s,
            cancel_token=cancel_token,
        )

    def render_provision(self, cfg: dict[str, Any]) -> RenderedProvision:
        """Render a first-boot bootstrap script for a remote Diffusers pod.

        The script is minimal: pip-install declared deps then ``exec``
        the server command so it becomes PID 1.  Credentials are not
        needed in the script itself — diffusers uses the HuggingFace
        Hub's own token cache (``HUGGINGFACE_HUB_TOKEN``) which the
        orchestrator can lift separately.

        Args:
            cfg: Runtime configuration dict.

        Returns:
            A :class:`RenderedProvision` ready for orchestrator wiring.
        """
        engine_block = cfg.get("engine", {})
        diffusers_cfg: dict[str, Any] = (
            engine_block.get("diffusers", {}) if isinstance(engine_block, dict) else {}
        )
        pip_deps: list[str] = list(diffusers_cfg.get("pip", []))
        server_cmd: list[str] = list(diffusers_cfg.get("server_cmd", []))
        base_url: str = str(diffusers_cfg.get("base_url", ""))
        image: str = str(diffusers_cfg.get("image", _DEFAULT_RUNPOD_IMAGE))
        embed_modules: list[str] = list(diffusers_cfg.get("embed_modules", []))
        embed_files: list[str] = list(diffusers_cfg.get("embed_files", []))
        # Derive WAN_MODEL_ID from cfg.models[<first-base>].ref so the
        # wan_t2v_server loads the actual cfg-declared repo rather than
        # its hardcoded 14B fallback. Without this, a Wan 2.1 1.3B cfg
        # silently provisions a 63GB Wan 2.2 14B load against a 24GB
        # A5000 and times out after ~15min during ``from_pretrained``.
        wan_model_id: str | None = None
        for model in cfg.get("models", []):
            if not isinstance(model, dict):
                continue
            if model.get("kind") != "base":
                continue
            ref = str(model.get("ref", ""))
            if ref.startswith("hf:"):
                wan_model_id = ref[len("hf:") :]
                break

        _preamble: list[str] = [
            "set -euo pipefail",
            # Capture all bootstrap stdout/stderr into a file the sidecar
            # log server (below) serves. Without this surface, Task 8
            # attempt #2/#3 restart-looped opaque to the orchestrator:
            # uptime cycled, GPU/CPU stayed flat, but the actual error
            # required SSH to retrieve. After this redirect every line
            # (pip install, embed decode noise, wan_t2v_server startup,
            # from_pretrained warnings) lands in /tmp/bootstrap.log.
            "exec > /tmp/bootstrap.log 2>&1",
            # Keep-alive trap. Without this, ANY bash exit (set -e abort,
            # pip-install failure, python crash after the main server
            # runs) terminates PID 1, the container dies, RunPod restarts
            # it, and the sidecar log server dies before it can serve
            # back the cause-of-death. ``sleep infinity`` parks bash
            # forever so the log server stays bound; selfterm's
            # dead-man window and ``max_lifetime`` caps still fire on
            # schedule, so this is NOT a runaway-cost risk. Registered
            # as the FIRST executable statement so even an http.server
            # bind failure (port collision, missing python3) is captured
            # before it can kill the container.
            'trap \'echo "[bootstrap-trap] rc=$? at $(date -u +%FT%TZ)";'
            " sleep infinity' EXIT",
            # Sidecar HTTP log server — serves /tmp/bootstrap.log (and
            # anything else under /tmp) over port 8001. Backgrounded
            # with nohup so it survives the main server's exit; own
            # stdout/stderr sunk to /dev/null so it never feeds back
            # into the log it serves. RunPod proxies port 8001 via the
            # pod's endpoints[] map.
            "nohup python3 -m http.server 8001 --directory /tmp >/dev/null 2>&1 &",
            # Selfterm watchdog — launch BEFORE pip-install so the
            # dead-man window + max-lifetime cap fire even when pip
            # hangs. Mirrors the ComfyUI engine's selfterm-launch
            # pattern.
            'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then '
            "python3 -c \"import os; open('/tmp/selfterm.py','w')"
            ".write(os.environ['KINOFORGE_SELFTERM_SCRIPT'])\" && "
            "nohup python3 /tmp/selfterm.py > /tmp/selfterm.log 2>&1 & "
            "fi",
        ]
        # Fast-boot split (2026-07-10): every appended segment is tagged either
        # "build" (bakeable install — pip, composed upscaler/interpolator) or
        # "runtime" (container-start — embed, exports, server exec). ``lines`` is
        # the combined stream RunPod still boots verbatim; the two buckets feed
        # RenderedProvision.build_script / runtime_script so Modal can bake the
        # installs into the image. The preamble is all runtime.
        lines: list[str] = list(_preamble)
        runtime_lines: list[str] = list(_preamble)
        build_lines: list[str] = []

        def _add(phase: str, *new: str) -> None:
            """Append line(s) to the combined stream AND the phase bucket(s).

            phase is "build", "runtime", or "both". "both" is for steps a baked
            image needs at BUILD time yet the runtime container also needs — the
            module embed: the composed FlashVSR weights-fetch runs
            ``python -m kinoforge...`` which resolves only against the embedded
            /tmp/kfsrv tree + PYTHONPATH, so that tree must exist in the image at
            bake time; the runtime server needs it too.
            """
            for ln in new:
                lines.append(ln)
                if phase in ("build", "both"):
                    build_lines.append(ln)
                if phase in ("runtime", "both"):
                    runtime_lines.append(ln)

        if embed_modules or embed_files:
            embed_lines: list[str] = []
            if embed_modules:
                embed_lines.extend(_render_embed_lines(embed_modules))
            for mod in embed_files:
                embed_lines.extend(
                    _render_embed_single_file(mod, "/tmp/kfsrv")  # noqa: S108
                )
            embed_lines.append("export PYTHONPATH=/tmp/kfsrv:${PYTHONPATH:-}")
            # "both": the baked image needs /tmp/kfsrv + PYTHONPATH so the
            # build-phase weights fetch (python -m kinoforge...) resolves; the
            # runtime server needs it too. Appears once in the combined script.
            _add("both", *embed_lines)
        if pip_deps:
            # shlex.quote each dep — pip version specifiers like
            # ``diffusers>=0.32`` contain a bare ``>=`` which bash
            # parses as a stdout redirect under ``set -euo pipefail``
            # (silently creating ``=0.32`` files and stripping the pin
            # from pip's argv). The fix burned ~$0.11 of pod-idle time
            # across a restart loop before being caught; see plan
            # amendment 2026-06-19 Task 8 attempt #2 post-mortem.
            quoted = " ".join(shlex.quote(d) for d in pip_deps)
            # ``--extra-index-url`` (not ``--index-url``) layers the
            # PyTorch wheel index alongside PyPI so ``torch>=2.6`` (or
            # similar) resolves to cu124 wheels while ``diffusers``,
            # ``transformers``, etc. still come from PyPI. Letting the
            # bootstrap upgrade torch in-place avoids the torch 2.8
            # cudnn image-pull stalls (Task 8 attempts #13-15) while
            # keeping the in-pod torch new enough to handle stringified
            # annotations in ``infer_schema`` (the diffusers Wan VAE
            # custom_op decorator). The flag is safe for cfgs that
            # don't upgrade torch — pip just ignores it for deps
            # already satisfied.
            #
            # Cfg override `engine.diffusers.pytorch_extra_index_url`
            # lets a cfg pin a different CUDA build — required by the
            # FlashVSR x4 cfg which needs cu128 to match the prebuilt
            # BSA wheel (`bsa-cu128-torch2.8-v1`).
            extra_index_url = str(
                diffusers_cfg.get("pytorch_extra_index_url", _PYTORCH_EXTRA_INDEX_URL)
            )
            _add(
                "build", f"pip install -q --extra-index-url {extra_index_url} {quoted}"
            )
        # T15 — upscale-only mode: bypass eager WanPipeline.from_pretrained
        # in the on-pod server. The composed upscaler render_provision (T8)
        # still fires below so spandrel weights land at /workspace/models/
        # spandrel; the LRU registry loads spandrel on the first /upscale.
        if diffusers_cfg.get("upscale_only"):
            _add("runtime", "export KINOFORGE_SKIP_WAN_LOAD=1")

        # T8 — compose upscaler render_provision script when cfg.upscale set.
        # Reads cfg.upscale.engine, looks up via registry, appends the upscaler's
        # render_provision script BEFORE the server exec line. Engine-agnostic:
        # this seam knows nothing about WHICH upscaler. FlashVSR drop-in (future)
        # gets composition for free. Raises ExtrasNotInstalled when the upscaler
        # is extras-gated (e.g. seedvr2 pre-Phase 2) — propagates to the
        # orchestrator's cfg-time pre-flight rather than crashing at pod boot.
        upscale_block_raw = cfg.get("upscale") if isinstance(cfg, dict) else None
        if isinstance(upscale_block_raw, dict):
            upscaler_name = upscale_block_raw.get("engine")
            if upscaler_name:
                from kinoforge.core import registry as _registry

                upscaler = _registry.get_upscaler(str(upscaler_name))()
                upscale_rp = upscaler.render_provision(cfg)
                _add(
                    "build",
                    "# ---- upscaler provision (composed) ----",
                    *(line for line in upscale_rp.script.split("\n") if line),
                )

        # Compose the interpolator render_provision when cfg.interpolate is set.
        # Mirrors the upscaler composition above: reads cfg.interpolate.engine,
        # resolves via the registry, and appends the interpolator's provision
        # script BEFORE the server exec line. Engine-agnostic — knows nothing
        # about WHICH interpolator (RIFE today, FILM/GIMM future drop-ins).
        interp_block_raw = cfg.get("interpolate") if isinstance(cfg, dict) else None
        if isinstance(interp_block_raw, dict):
            interp_name = interp_block_raw.get("engine")
            if interp_name:
                from kinoforge.core import registry as _registry

                interpolator = _registry.get_interpolator(str(interp_name))()
                interp_rp = interpolator.render_provision(cfg)
                _add(
                    "build",
                    "# ---- interpolator provision (composed) ----",
                    *(line for line in interp_rp.script.split("\n") if line),
                )

        if wan_model_id and server_cmd:
            # Exported BEFORE server_cmd so the launching shell carries
            # it into the wan_t2v_server process. See wan_t2v_server.py
            # MODEL_ID = os.environ.get("WAN_MODEL_ID", "<14B-default>").
            _add("runtime", f"export WAN_MODEL_ID={shlex.quote(wan_model_id)}")
        if server_cmd:
            # NOTE: NO `exec` prefix. Bash must remain PID 1 so the
            # EXIT trap can fire when the main server crashes (or
            # exits cleanly). Replacing bash with python via ``exec``
            # would strand the trap and the container would die at
            # the first python error.
            _add("runtime", " ".join(server_cmd))

        port = _extract_port_from_base_url(base_url)
        # 8001 is the sidecar log-server port; emitted alongside the main
        # server port so the provider exposes both via its proxy URLs.
        ports = [port, "8001"] if port != "8001" else [port]
        build_script = ""
        if build_lines:
            # build_lines run at image-BUILD (Modal) — give them their own
            # fail-fast preamble (the combined script's set -e lives in the
            # runtime preamble, which the baked image does not run).
            build_script = "set -euo pipefail\n" + "\n".join(build_lines)
        runtime_script = "\n".join(runtime_lines)
        return RenderedProvision(
            script="\n".join(lines),
            build_script=build_script,
            runtime_script=runtime_script,
            run_cmd=server_cmd,
            image=image,
            ports=ports,
            # HF_TOKEN is required because huggingface_hub's anonymous
            # rate-limit kills mid-download for large repos. Task 8
            # attempt #9 surfaced this: ``Fetching 41 files`` stuck at
            # 3/41 for 4+ minutes with the "set a HF_TOKEN to enable
            # higher rate limits and faster downloads" warning. The
            # orchestrator lifts HF_TOKEN from creds (the .env file
            # under workspace) and the RunPod provider injects it as
            # a pod env var so wan_t2v_server.py's from_pretrained
            # uses the authenticated transport.
            env_required=["HF_TOKEN"],
        )

    def wait_for_ready(
        self,
        instance: Instance,
        *,
        http_get: Callable[[str], dict[str, Any]],
        sleep: Callable[[float], None],
        get_instance: Callable[[str], Instance],
        timeout_s: float,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """Poll ``GET <base_url>/health`` until 200, terminal status, or timeout.

        Mirror of :meth:`ComfyUIEngine.wait_for_ready` with the
        Diffusers-specific ready URL (``/health`` on port ``8000``).

        Args:
            instance: The just-created compute instance.
            http_get: HTTP GET seam — raises on error, returns dict on success.
            sleep: Sleep seam used between polls.
            get_instance: Provider lookup for status checks between polls.
            timeout_s: Maximum total wait.
            cancel_token: C29 cooperative cancellation. Checked at the top of
                each poll iteration before any I/O. Default ``None`` preserves
                pre-C29 behaviour.

        Raises:
            ProvisionFailed: Pod entered terminal status before ready.
            ProvisionTimeout: ``timeout_s`` elapsed without a successful ready check.
            Cancelled: ``cancel_token`` was set during the wait.
        """
        if not instance.endpoints:
            raise ProvisionFailed(
                f"pod {instance.id!r} has no endpoints — cannot construct ready URL"
            )
        port_key = (
            "8000"
            if "8000" in instance.endpoints
            else next(iter(instance.endpoints), "8000")
        )
        base = instance.endpoints.get(port_key, "")
        ready_url = f"{base.rstrip('/')}/health"

        start = time.monotonic()
        last_probe = start - _BOOT_PROBE_INTERVAL_S  # allow a probe on first idle poll
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_set()
            now = time.monotonic()
            if now - start >= timeout_s:
                raise ProvisionTimeout(
                    f"engine ready check timed out after {timeout_s:.0f}s "
                    f"for pod {instance.id!r}"
                )
            try:
                http_get(ready_url)
                return
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                current = get_instance(instance.id)
            except KeyError as exc:
                raise ProvisionFailed(
                    f"pod {instance.id!r} vanished during boot (provider "
                    f"no longer knows it)"
                ) from exc
            if current.status in ("terminated", "stopped"):
                raise ProvisionFailed(
                    f"pod {instance.id!r} entered terminal status "
                    f"{current.status!r} before ready"
                )
            # 2026-07-07 boot-stall fast-fail: consult the injected liveness
            # probe on its own throttle (not every /health poll). GONE/STALLED
            # abort in ~2-3min instead of waiting the full boot_timeout.
            probe = getattr(self, "_boot_liveness_probe", None)
            if probe is not None and now - last_probe >= _BOOT_PROBE_INTERVAL_S:
                last_probe = now
                verdict = probe.check(instance.id)
                if verdict is BootVerdict.GONE:
                    raise ProvisionFailed(f"pod {instance.id!r} vanished during boot")
                if verdict is BootVerdict.STALLED:
                    raise ProvisionFailed(
                        f"pod {instance.id!r} boot stalled (provision crashed "
                        f"or util flatline) — aborting before boot_timeout"
                    )
            sleep(_READY_POLL_INTERVAL_S)

    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> DiffusersBackend:
        """Return a :class:`DiffusersBackend` wired to this engine's injected callables.

        The base URL is taken from
        ``cfg["engine"]["diffusers"]["base_url"]`` when present, otherwise
        defaults to :data:`_DEFAULT_BASE_URL`.

        Args:
            instance: The compute instance (unused currently).
            cfg: Runtime configuration dict.

        Returns:
            A :class:`DiffusersBackend` ready to accept jobs.
        """
        engine_block = cfg.get("engine", {})
        diffusers_cfg: dict[str, Any] = (
            engine_block.get("diffusers", {}) if isinstance(engine_block, dict) else {}
        )
        # Remote pod: use the proxy URL from instance.endpoints, not the
        # cfg's base_url. cfg.base_url ("http://localhost:8000") is only
        # valid when DiffusersEngine runs LOCAL (provider=local). For
        # remote pods, the proxy URL was set on instance.endpoints by
        # the provider at create_instance time. Task 8 attempt #24
        # surfaced this gap with a Connection refused error on
        # http://localhost:8000/generate from the workspace container.
        cfg_base_url: str = str(diffusers_cfg.get("base_url", _DEFAULT_BASE_URL))
        if instance is not None and instance.provider != "local" and instance.endpoints:
            port = _extract_port_from_base_url(cfg_base_url)
            base_url = instance.endpoints.get(port) or instance.endpoints.get(
                "8000", cfg_base_url
            )
        else:
            base_url = cfg_base_url
        asset_paths_raw = diffusers_cfg.get("asset_paths", {})
        asset_paths: dict[str, str] = (
            {str(k): str(v) for k, v in asset_paths_raw.items()}
            if isinstance(asset_paths_raw, dict)
            else {}
        )
        # Mirror onto the engine so ``validate_spec`` (called from outside
        # via the ABC) can consult the same map.
        self._asset_paths = asset_paths
        prompt_body_key_raw = diffusers_cfg.get("prompt_body_key", "prompt")
        prompt_body_key: str | None = (
            prompt_body_key_raw
            if isinstance(prompt_body_key_raw, str) and prompt_body_key_raw
            else None
        )
        self._prompt_body_key = prompt_body_key
        return DiffusersBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe_profile=self._probe,
            sleep=self._sleep,
            asset_paths=asset_paths,
            prompt_body_key=prompt_body_key,
            poll_timeout_s=float(diffusers_cfg.get("poll_timeout_s", 1800.0)),
            poll_interval_s=float(diffusers_cfg.get("poll_interval_s", 1.0)),
        )

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Raise ``NotImplementedError`` — deferred to the profile provider.

        Args:
            key: Unused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "DiffusersEngine.profile_for is supplied by ModelProfileProvider"
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        """Return the strategy flags declared for *key*, or ``{}`` if unknown.

        Args:
            key: The :class:`~kinoforge.core.interfaces.CapabilityKey` to
                look up.

        Returns:
            A ``dict[str, bool]`` of strategy flags, or ``{}`` when no entry
            exists for this key.
        """
        return dict(self._declared_flags_map.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        """Raise :class:`~kinoforge.core.errors.ValidationError` for spec or asset gaps.

        Required spec keys: ``"pipeline"`` (the diffusers pipeline class
        name) and ``"scheduler"`` (the scheduler name).  In addition, for
        every asset in ``job.segments[0].assets``, the asset's role must
        appear in ``self._asset_paths`` (populated from
        ``cfg["engine"]["diffusers"]["asset_paths"]`` at backend
        construction).  Roles present without a configured injection path
        are a hard error — silent skip would let the engine submit a body
        missing the conditioning asset.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
                ``spec`` is checked.

        Raises:
            ValidationError: ``"pipeline"`` or ``"scheduler"`` is absent
                from ``job.spec``, or an asset role on ``segments[0]`` has
                no entry in ``asset_paths``.
        """
        required = {"pipeline", "scheduler"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"Diffusers job.spec is missing required keys: {sorted(missing)}"
            )
        if self._prompt_body_key:
            from kinoforge.core.prompt_routing import resolve_prompt

            if resolve_prompt(job) is None:
                raise ValidationError(
                    "diffusers prompt_body_key is configured but no prompt found in "
                    "job.spec or segments[0] — set spec.prompt, set "
                    "segments[0].prompt, or disable routing with "
                    "engine.diffusers.prompt_body_key: null"
                )
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role not in self._asset_paths:
                raise ValidationError(
                    f"asset role {asset.role!r} present on segments[0] but "
                    f"engine.diffusers.asset_paths has no mapping; declare "
                    f"asset_paths.{asset.role}: <dot.path> in YAML"
                )

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Diffusers identity matches the hosted pattern — ``spec.model``."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the rendered video bytes via HTTP and decode the last frame.

        Args:
            artifact: A clip Artifact returned by :meth:`DiffusersBackend.result`
                with ``url`` populated by the inference server.

        Returns:
            PNG-encoded last frame as bytes.

        Raises:
            FrameExtractionError: ``artifact.url`` is empty (server omitted
                the ``url`` field from its response), or the fetch or ffmpeg
                decode failed.
        """
        if not artifact.url:
            raise FrameExtractionError(
                f"{type(self).__name__}: artifact.url is empty; "
                "cannot fetch video bytes"
            )
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY,
            retry_proxy_call,
        )

        try:
            video_bytes = retry_proxy_call(
                "diffusers.artifact",
                artifact.url,
                lambda: self._http_get_bytes(artifact.url),
                self._sleep,
                RUNPOD_PROXY_POLICY,
            )
        except Exception as exc:
            raise FrameExtractionError(
                f"{type(self).__name__}: fetch from {artifact.url!r} failed: {exc}"
            ) from exc
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("diffusers", lambda: DiffusersEngine())
