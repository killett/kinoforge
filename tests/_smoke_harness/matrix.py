"""Engine-agnostic 4-step matrix runner.

Drives the same shape (set_stack → inventory check → optional
generate → sha capture) across every smoke tier. Tier 1 passes
``generate_per_step=False`` (HTTP-only); Tiers 3/4 pass ``True``
to validate end-to-end output.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests._smoke_harness import http


@dataclass(frozen=True)
class MatrixStep:
    name: str
    target_stack: list[str]
    expected_inventory: list[str]
    expected_evict: list[str] | None = None
    expected_download: list[str] | None = None


@dataclass
class StepResult:
    name: str
    inventory_after: list[str]
    mp4_path: Path | None
    mp4_sha: str | None
    wall_clock_s: float


@dataclass
class MatrixReport:
    steps: list[StepResult]


def _run_generate(cfg: Path, pod_id: str, prompt: str) -> Path:
    """Invoke ``kinoforge generate --instance-id <pod>``; return mp4 path."""
    proc = subprocess.run(
        [
            "pixi",
            "run",
            "kinoforge",
            "generate",
            "--config",
            str(cfg),
            "--prompt",
            prompt,
            "--mode",
            "t2v",
            "--instance-id",
            pod_id,
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, f"generate failed: {proc.stdout}\n{proc.stderr}"
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("generated: uri="):
            uri = line.split("=", 1)[1].strip().strip("'\"")
            return Path(uri)
    raise AssertionError(f"no 'generated:' line in CLI output:\n{proc.stdout}")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _warmup_proxy(pod_proxy_url: str, *, deadline_s: float = 60.0) -> None:
    """Probe /health until 200 before issuing any /lora/* POST.

    Defends against a recurring RunPod-edge-proxy bug observed across
    Tier-3 fires #5-#7: the FIRST POST after a freshly-created pod
    intermittently returns "Waiting for service to respond" (HTTP 502)
    even though uvicorn is listening. A preceding /health GET warms
    the proxy's upstream connection — subsequent /lora/set_stack calls
    then succeed in seconds (diag run 2026-06-21 confirmed: 200 in 8.1s).

    Args:
        pod_proxy_url: ``https://{pod_id}-{port}.proxy.runpod.net``
            base.
        deadline_s: How long to keep retrying before raising.
    """
    deadline = time.monotonic() + deadline_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            http.get_json(f"{pod_proxy_url.rstrip('/')}/health", timeout=10)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(2.0)
    raise RuntimeError(
        f"proxy warmup failed: /health never returned 200 within "
        f"{deadline_s}s — last error {last_exc!r}"
    )


def _wait_for_inventory_convergence(
    *,
    pod_proxy_url: str,
    target: list[str],
    deadline_s: float,
    step_name: str,
) -> list[str]:
    """Poll /lora/inventory until refs match ``target`` (or deadline).

    Called from run_matrix when /lora/set_stack returns a proxy 502 —
    the upstream wan server may still be processing the download. We
    poll every 10s for up to ``deadline_s`` and return the converged
    inventory; if convergence never happens, raise AssertionError with
    the last observed state so the caller's matrix step fails with
    actionable signal.
    """
    target_sorted = sorted(target)
    deadline = time.monotonic() + deadline_s
    last_observed: list[str] = []
    while time.monotonic() < deadline:
        time.sleep(10.0)
        try:
            resp = http.get_json(
                f"{pod_proxy_url.rstrip('/')}/lora/inventory",
                timeout=30,
            )
        except urllib.error.URLError:
            # Treat transient probe failures as "still working".
            continue
        last_observed = sorted(e["ref"] for e in resp.get("inventory", []))
        if last_observed == target_sorted:
            return last_observed
    raise AssertionError(
        f"{step_name}: set_stack 502 + inventory did not converge to "
        f"{target_sorted} within {deadline_s}s — last observed {last_observed}"
    )


def run_matrix(
    *,
    cfg_path: Path,
    pod_proxy_url: str,
    steps: list[MatrixStep],
    download_specs: dict[str, dict[str, Any]],
    generate_per_step: bool = True,
    sha_distinct_required: bool = True,
    pod_id: str | None = None,
    prompt: str = "smoke test prompt",
) -> MatrixReport:
    """Run the steps in order; return per-step results.

    Args:
        cfg_path: kinoforge cfg path for the per-step ``kinoforge
            generate --instance-id`` invocations (ignored when
            ``generate_per_step=False``).
        pod_proxy_url: ``https://{pod_id}-{port}.proxy.runpod.net``
            (or ``http://localhost:{port}`` for Tier 1).
        steps: Ordered list of ``MatrixStep`` to execute.
        download_specs: ``ref -> {url, headers, filename, size_hint}``
            superset; runner slices per step.
        generate_per_step: When True, runs ``kinoforge generate
            --instance-id`` after each set_stack to capture an mp4.
            Tier 1 toggles False.
        sha_distinct_required: When True + ``generate_per_step=True``,
            adjacent step mp4 shas must differ.
        pod_id: Required when ``generate_per_step=True``.
        prompt: Prompt passed to each ``kinoforge generate``.

    Returns:
        ``MatrixReport`` with one ``StepResult`` per step.

    Raises:
        AssertionError: When a step's post-state ``inventory`` does
            not equal ``MatrixStep.expected_inventory``, OR (with
            distinct-sha) two adjacent mp4s hash identically.
    """
    _warmup_proxy(pod_proxy_url)
    results: list[StepResult] = []
    prev_sha: str | None = None
    for step in steps:
        t0 = time.monotonic()
        sliced = {ref: download_specs[ref] for ref in step.target_stack}
        try:
            resp = http.post_json(
                f"{pod_proxy_url.rstrip('/')}/lora/set_stack",
                {
                    "target_refs": step.target_stack,
                    "download_specs": sliced,
                },
                timeout=900,
            )
            observed = sorted(e["ref"] for e in resp.get("inventory", []))
        except urllib.error.HTTPError as exc:
            # RunPod proxy 502s long downloads (~100s ceiling) even
            # when the upstream wan server is still streaming bytes.
            # Recover by polling /lora/inventory until it converges to
            # the target stack (or we give up). Treats proxy timeout
            # as a soft failure — the actual set_stack handler keeps
            # running pod-side until _download_one returns or the
            # in-handler 600s timeout fires.
            if exc.code != 502:
                raise
            observed = _wait_for_inventory_convergence(
                pod_proxy_url=pod_proxy_url,
                target=step.target_stack,
                deadline_s=600.0,
                step_name=step.name,
            )
        assert observed == sorted(step.expected_inventory), (
            f"{step.name}: inventory mismatch — "
            f"expected {sorted(step.expected_inventory)}, got {observed}"
        )
        mp4_path: Path | None = None
        mp4_sha: str | None = None
        if generate_per_step:
            assert pod_id is not None, "pod_id required when generate_per_step=True"
            mp4_path = _run_generate(cfg_path, pod_id, prompt)
            mp4_sha = _sha256(mp4_path)
            if sha_distinct_required and prev_sha is not None:
                assert mp4_sha != prev_sha, (
                    f"{step.name}: mp4 sha matches previous step — "
                    f"LoRA swap had no measurable effect on output"
                )
            prev_sha = mp4_sha
        results.append(
            StepResult(
                name=step.name,
                inventory_after=observed,
                mp4_path=mp4_path,
                mp4_sha=mp4_sha,
                wall_clock_s=time.monotonic() - t0,
            )
        )
    return MatrixReport(steps=results)
