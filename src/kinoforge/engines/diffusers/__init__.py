"""DiffusersEngine + DiffusersBackend — adapter for a locally-running diffusers inference server.

All I/O (subprocess, HTTP, sleep) is routed through injected callables so that
tests can spy without any real side-effects.

Module-level self-registration puts the factory in the global registry under
the ``"diffusers"`` key so that ``registry.get_engine("diffusers")()`` works.
"""

from __future__ import annotations

import base64
import importlib.resources
import json
import re
import shlex
import subprocess
import time
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kinoforge.core import frames, registry

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
from kinoforge.core.assets import find_asset, set_by_dot_path
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum poll iterations in :meth:`DiffusersBackend.result`.
_MAX_POLL = 60

#: Default server base URL.
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: Default container image for remote provisioning.
_DEFAULT_RUNPOD_IMAGE: str = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

#: Seconds between readiness polls in :meth:`DiffusersEngine.wait_for_ready`.
_READY_POLL_INTERVAL_S: float = 5.0


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
        pkg_root = importlib.resources.files(mod_name)
        for resource in pkg_root.iterdir():
            if not resource.is_file() or not resource.name.endswith(".py"):
                continue
            rel = mod_name.replace(".", "/") + "/" + resource.name
            target = f"{kfsrv}/{rel}"
            content = resource.read_bytes()
            encoded = base64.b64encode(content).decode("ascii")
            lines.append(f"echo '{encoded}' | base64 -d > {target}")
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
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
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
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


def _urllib_get_bytes(url: str) -> bytes:
    """GET *url* and return the raw response body as bytes.

    Args:
        url: Endpoint URL.

    Returns:
        Response body as bytes.
    """
    with urllib.request.urlopen(url) as resp:  # noqa: S310
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
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe_profile
        self._sleep = sleep
        self._asset_paths: dict[str, str] = dict(asset_paths or {})
        self._prompt_body_key: str | None = prompt_body_key

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
        response = self._http_post(url, body)
        return str(response["job_id"])

    def result(
        self,
        job_id: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact:
        """Poll ``/status/{job_id}`` until ``status == "done"``.

        Polls at most :data:`_MAX_POLL` times, sleeping between iterations
        using the injected *sleep* callable.

        Args:
            job_id: The job ID returned by a prior ``submit`` call.
            cancel_token: Accepted for :class:`GenerationBackend` ABC
                parity; ignored at this layer. Full cooperative honoring
                of the poll loop is added by a follow-up layer.

        Returns:
            An ``Artifact`` whose ``filename`` comes from the server response
            and whose ``meta`` contains ``{"job_id": job_id}``.

        Raises:
            TimeoutError: The server did not return ``status == "done"``
                within the poll limit.
        """
        del cancel_token  # ABC parity; full honoring deferred to a future task.
        from kinoforge.core.errors import GenerationError  # local — avoid circular

        url = f"{self._base_url}/status/{job_id}"
        for _ in range(_MAX_POLL):
            data = self._http_get(url)
            status = data.get("status")
            if status == "done":
                filename = str(data.get("filename", ""))
                artifact_url = str(data.get("url", ""))
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
            self._sleep(1.0)
        raise TimeoutError(
            f"Diffusers server did not complete job {job_id!r} within {_MAX_POLL} polls"
        )

    def endpoints(self) -> dict[str, str]:
        """Return the diffusers server endpoint map.

        Returns:
            A dict with ``"generate"`` and ``"status"`` endpoint URLs.
        """
        return {
            "generate": f"{self._base_url}/generate",
            "status": f"{self._base_url}/status",
        }


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

        lines: list[str] = [
            "set -euo pipefail",
            # Selfterm watchdog — launch BEFORE pip-install so the dead-man
            # window + max-lifetime cap fire even when pip hangs. Mirrors
            # the ComfyUI engine's selfterm-launch pattern.
            'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then '
            "python3 -c \"import os; open('/tmp/selfterm.py','w')"
            ".write(os.environ['KINOFORGE_SELFTERM_SCRIPT'])\" && "
            "nohup python3 /tmp/selfterm.py > /tmp/selfterm.log 2>&1 & "
            "fi",
        ]
        if embed_modules:
            lines.extend(_render_embed_lines(embed_modules))
            lines.append("export PYTHONPATH=/tmp/kfsrv:${PYTHONPATH:-}")
        if pip_deps:
            # shlex.quote each dep — pip version specifiers like
            # ``diffusers>=0.32`` contain a bare ``>=`` which bash parses
            # as a stdout redirect under ``set -euo pipefail`` (silently
            # creating ``=0.32`` files and stripping the pin from pip's
            # argv). The fix burned ~$0.11 of pod-idle time across a
            # restart loop before being caught; see plan amendment
            # 2026-06-19 Task 8 attempt #2 post-mortem.
            quoted = " ".join(shlex.quote(d) for d in pip_deps)
            lines.append(f"pip install -q {quoted}")
        if server_cmd:
            lines.append("exec " + " ".join(server_cmd))

        port = _extract_port_from_base_url(base_url)
        return RenderedProvision(
            script="\n".join(lines),
            run_cmd=server_cmd,
            image=image,
            ports=[port],
            env_required=[],
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
            current = get_instance(instance.id)
            if current.status in ("terminated", "stopped"):
                raise ProvisionFailed(
                    f"pod {instance.id!r} entered terminal status "
                    f"{current.status!r} before ready"
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
        del instance
        engine_block = cfg.get("engine", {})
        diffusers_cfg: dict[str, Any] = (
            engine_block.get("diffusers", {}) if isinstance(engine_block, dict) else {}
        )
        base_url: str = str(diffusers_cfg.get("base_url", _DEFAULT_BASE_URL))
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
        try:
            video_bytes = self._http_get_bytes(artifact.url)
        except Exception as exc:
            raise FrameExtractionError(
                f"{type(self).__name__}: fetch from {artifact.url!r} failed: {exc}"
            ) from exc
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("diffusers", lambda: DiffusersEngine())
