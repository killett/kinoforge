r"""Diagnostic: boot a RunPod pod with prod bootstrap; poll runtime + logs.

T4 attempts 5 and 6 (HEAD 94e0d2c) both timed out in
``wait_for_ready`` polling ``/system_stats`` despite a 30 min budget.
The pod's actual state — bootstrap stdout, port exposure, container
status — is invisible from the controller process. This tool fills that
gap by polling RunPod's REST API directly:

* ``GET /v1/pods/{id}`` — full pod metadata + ``runtime`` subobject
* ``GET /v1/pods/{id}/logs`` — container stdout/stderr (if the endpoint
  exists; falls back to a notice if 404)
* every 30 s for up to ``--max-minutes`` minutes
* always destroys the pod in a ``finally`` block

Cost: ``--max-minutes`` × ``costPerHr`` of the cheapest offer. Default
5 min × $0.27/hr ≈ $0.022 per diagnostic run.

Usage::

    pixi run python tools/diagnose_pod_boot.py \\
        --workflow-yaml examples/configs/runpod-comfyui-wan.yaml \\
        [--max-minutes 5]

The bootstrap is rendered from the same ``ComfyUIEngine.render_provision``
production path so the diagnostic is faithful to the T4 failure mode.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools._redact import redact_string, safe_print  # noqa: E402

_UA = "kinoforge-diagnose/1.0"


def _rest_call(
    url: str,
    api_key: str,
    *,
    method: str = "GET",
) -> tuple[int, str]:
    """Call RunPod REST API; return (status, body_text)."""
    req = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _redact_pod_record(pod: dict[str, Any]) -> dict[str, Any]:
    """Return *pod* with env stripped + string fields scrubbed."""
    out: dict[str, Any] = {}
    for k, v in pod.items():
        if k == "env":
            out[k] = f"<{len(v) if isinstance(v, dict) else '?'} keys redacted>"
        elif isinstance(v, str):
            out[k] = redact_string(v)
        else:
            out[k] = v
    return out


def _print_pod_snapshot(api_key: str, pod_id: str, elapsed_s: int) -> dict[str, Any]:
    """Fetch + print current pod state. Returns the parsed pod dict."""
    code, body = _rest_call(f"https://rest.runpod.io/v1/pods/{pod_id}", api_key)
    print(f"\n--- t={elapsed_s}s: GET /v1/pods/{pod_id} -> {code} ---")
    if code != 200:
        print(redact_string(body[:600]))
        return {}
    pod = json.loads(body)
    if not isinstance(pod, dict):
        print(f"  unexpected response shape: {type(pod).__name__}")
        return {}
    safe_fields = (
        "id",
        "name",
        "desiredStatus",
        "lastStatusChange",
        "createdAt",
        "costPerHr",
        "imageName",
        "containerDiskInGb",
        "machineId",
    )
    print("  state:")
    for f in safe_fields:
        if f in pod:
            print(f"    {f}={pod[f]!r}")
    runtime = pod.get("runtime") or {}
    if runtime:
        print(f"  runtime: {json.dumps(runtime, sort_keys=True)[:400]}")
    else:
        print("  runtime: <not yet populated>")
    ports = pod.get("portMappings") or pod.get("ports") or runtime.get("ports")
    if ports is not None:
        print(f"  ports: {ports!r}")
    return pod


def _print_pod_logs(api_key: str, pod_id: str) -> None:
    """Try the REST logs endpoint; print last 40 lines."""
    code, body = _rest_call(f"https://rest.runpod.io/v1/pods/{pod_id}/logs", api_key)
    print(f"  GET /v1/pods/{pod_id}/logs -> {code}")
    if code == 404:
        print("    (no logs endpoint — RunPod REST may not support this)")
        return
    if code != 200:
        print(f"    {redact_string(body[:300])}")
        return
    # Try JSON first, fall back to raw text
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "logs" in parsed:
            log_text = str(parsed["logs"])
        else:
            log_text = body
    except json.JSONDecodeError:
        log_text = body
    lines = log_text.splitlines()
    tail = lines[-40:] if len(lines) > 40 else lines
    print(f"  logs (last {len(tail)} of {len(lines)} lines):")
    for ln in tail:
        print(f"    | {redact_string(ln)[:200]}")


def main() -> int:
    """CLI entrypoint. See module docstring."""
    parser = argparse.ArgumentParser(prog="diagnose_pod_boot")
    parser.add_argument("--workflow-yaml", required=True, type=Path)
    parser.add_argument("--max-minutes", type=int, default=5)
    parser.add_argument("--env-file", type=Path, default=Path(_REPO_ROOT) / ".env")
    parser.add_argument("--poll-interval-s", type=int, default=30)
    parser.add_argument(
        "--minimal-phonehome",
        action="store_true",
        help=(
            "Replace production bootstrap with a tiny phone-home script that "
            "writes step markers to /workspace/progress.log then serves "
            "/workspace via python3 -m http.server on port 8188. "
            "Controller polls the proxy URL for the log. Used to test whether "
            "dockerArgs actually executes the bash script at all."
        ),
    )
    parser.add_argument(
        "--image-override",
        type=str,
        default=None,
        help="Override the image (used to test whether the YAML image's ENTRYPOINT eats dockerArgs).",
    )
    parser.add_argument(
        "--ports-protocol",
        type=str,
        default=None,
        help="Append /<protocol> to each declared port (e.g. 'http' -> '8188/http').",
    )
    parser.add_argument(
        "--instrument-production",
        action="store_true",
        help=(
            "Run the production Wan bootstrap step-by-step, wrapping each "
            "command with timestamped progress markers + a diagnostic "
            "http.server on port 9000 serving /workspace/diag/. Lets us "
            "watch which step hangs/fails via the proxy URL."
        ),
    )
    args = parser.parse_args()

    from kinoforge.core.dotenv_loader import load_env_file

    if args.env_file.exists():
        load_env_file(args.env_file)

    for k in ("RUNPOD_API_KEY", "RUNPOD_TERMINATE_KEY", "HF_TOKEN"):
        if not os.environ.get(k):
            safe_print(f"diagnose_pod_boot: missing env var: {k}")
            return 1
    api_key = os.environ["RUNPOD_API_KEY"]

    import kinoforge._adapters  # noqa: F401  registers engines/providers/sources
    from kinoforge.core import registry
    from kinoforge.core.config import load_config
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import InstanceSpec

    cfg = load_config(args.workflow_yaml)
    if cfg.engine.comfyui is None or cfg.compute is None:
        safe_print("diagnose_pod_boot: workflow YAML lacks comfyui / compute block")
        return 1

    engine = registry.get_engine(cfg.engine.kind)()
    engine.requires_local_weights = False
    provider = registry.get_provider(cfg.compute.provider)()
    provider._creds = EnvCredentialProvider()  # type: ignore[attr-defined]

    # Mirror capture_object_info's ref->src shim so render_provision can
    # resolve model entries against today's schema.
    from kinoforge.core.interfaces import RenderedProvision

    _orig_render = engine.render_provision

    def _shim(cfg_dict: dict[str, object]) -> RenderedProvision:
        models = cfg_dict.get("models") or []
        if isinstance(models, list):
            patched = [
                (
                    {**e, "src": e["ref"]}
                    if isinstance(e, dict) and "src" not in e and "ref" in e
                    else e
                )
                for e in models
            ]
            cfg_dict = {**cfg_dict, "models": patched}
        return _orig_render(cfg_dict)

    engine.render_provision = _shim  # type: ignore[assignment,method-assign]

    cfg_dict = cfg.model_dump()
    import dataclasses as _dc

    cfg_dict["lifecycle"] = _dc.asdict(cfg.lifecycle())
    rendered = engine.render_provision(cfg_dict)

    if args.minimal_phonehome:
        phonehome = (
            "set -euo pipefail\n"
            'echo "[step 1] bootstrap started: $(date -u)" > /workspace/progress.log\n'
            'echo "[step 2] uname=$(uname -a)" >> /workspace/progress.log\n'
            'echo "[step 3] pwd=$(pwd)" >> /workspace/progress.log\n'
            'echo "[step 4] which python3=$(which python3 || echo NONE)" >> /workspace/progress.log\n'
            'echo "[step 5] which bash=$(which bash || echo NONE)" >> /workspace/progress.log\n'
            'echo "[step 6] env-vars present: SELFTERM=${KINOFORGE_SELFTERM_SCRIPT:+yes} '
            'HF=${HF_TOKEN:+yes} POD_ID=${RUNPOD_POD_ID:-NONE}" >> /workspace/progress.log\n'
            'echo "[step 7] starting http.server on 8188" >> /workspace/progress.log\n'
            "cd /workspace\n"
            "exec python3 -m http.server 8188\n"
        )
        from kinoforge.core.interfaces import RenderedProvision as _RP

        port_str = "8188" + (f"/{args.ports_protocol}" if args.ports_protocol else "")
        rendered = _RP(
            script=phonehome,
            run_cmd=["python3", "-m", "http.server", "8188"],
            image=args.image_override or rendered.image,
            ports=[port_str],
            env_required=[],
        )

    if args.instrument_production:
        instrumented = (
            "set -uo pipefail\n"
            'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then '
            "python3 -c \"import os; open('/tmp/selfterm.py','w')"
            ".write(os.environ['KINOFORGE_SELFTERM_SCRIPT'])\" && "
            "nohup python3 /tmp/selfterm.py > /tmp/selfterm.log 2>&1 & "
            "fi\n"
            "mkdir -p /workspace/diag\n"
            'log() { echo "T+$(date +%s) $(date -u +%H:%M:%S): $*" '
            ">> /workspace/diag/progress.log; }\n"
            "log 'STEP 0: bootstrap started'\n"
            "cd /workspace/diag && "
            "nohup python3 -m http.server 9000 > /tmp/diag-server.log 2>&1 &\n"
            "sleep 2\n"
            "log 'STEP 0: diag http.server should be up on 9000'\n"
            "cd /workspace\n"
            "log 'STEP 1: git clone ComfyUI'\n"
            "if [ ! -d ComfyUI ]; then "
            "git clone --depth 1 --branch master "
            "https://github.com/comfyanonymous/ComfyUI ComfyUI "
            "> /workspace/diag/step1_clone.log 2>&1; "
            'log "STEP 1: exit=$?"; '
            "else log 'STEP 1: skipped'; fi\n"
            "log 'STEP 2: pip install ComfyUI reqs'\n"
            "cd /workspace/ComfyUI && pip install -q -r requirements.txt "
            "> /workspace/diag/step2_pip.log 2>&1; "
            'log "STEP 2: exit=$?"\n'
            "cd /workspace\n"
            "log 'STEP 3: clone WanVideoWrapper'\n"
            "if [ ! -d ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper ]; then "
            "git clone https://github.com/kijai/ComfyUI-WanVideoWrapper "
            "ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper "
            "> /workspace/diag/step3_wan_clone.log 2>&1; "
            'log "STEP 3: exit=$?"; fi\n'
            "log 'STEP 4: WanVideoWrapper pip reqs'\n"
            "if [ -f ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt ]; then "
            "pip install -q -r "
            "ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper/requirements.txt "
            "> /workspace/diag/step4_wan_pip.log 2>&1; "
            'log "STEP 4: exit=$?"; else log "STEP 4: no reqs.txt"; fi\n'
            "log 'STEP 4b: write kijai class probe script'\n"
            "cat > /tmp/kijai_probe.py <<'PYEOF'\n"
            "import importlib.util\n"
            "import sys\n"
            "import traceback\n"
            "sys.path.insert(0, '/workspace/ComfyUI')\n"
            "sys.path.insert(0, '/workspace/ComfyUI/custom_nodes')\n"
            "INIT_PATH = ('/workspace/ComfyUI/custom_nodes/'\n"
            "             'ComfyUI-WanVideoWrapper/__init__.py')\n"
            "try:\n"
            "    spec = importlib.util.spec_from_file_location('kijai_wan', INIT_PATH)\n"
            "    m = importlib.util.module_from_spec(spec)\n"
            "    spec.loader.exec_module(m)\n"
            "    mapping = getattr(m, 'NODE_CLASS_MAPPINGS', {})\n"
            "    print(f'KIJAI_INIT_OK classes={len(mapping)}')\n"
            "    for k in sorted(mapping):\n"
            "        print(f'  CLASS: {k}')\n"
            "except Exception as e:\n"
            "    print(f'KIJAI_INIT_ERROR: {type(e).__name__}: {str(e)[:800]}')\n"
            "    traceback.print_exc()\n"
            "PYEOF\n"
            "python3 /tmp/kijai_probe.py > /workspace/diag/step4b_kijai_probe.log 2>&1; "
            'log "STEP 4b: kijai_probe exit=$?"\n'
            "log 'STEP 5: launch ComfyUI in background'\n"
            "cd /workspace/ComfyUI && "
            "nohup python main.py --listen 0.0.0.0 --port 8188 "
            "> /workspace/diag/step5_comfy.log 2>&1 &\n"
            'log "STEP 5: ComfyUI launched pid=$!"\n'
            "log 'STEP 6: probing local /system_stats every 15s for 15 min'\n"
            "for i in $(seq 1 60); do "
            "if curl -sf http://127.0.0.1:8188/system_stats "
            "> /workspace/diag/step6_curl.log 2>&1; then "
            'log "STEP 6: ComfyUI READY at iter=$i (~$((i*15))s)"; '
            "break; "
            "fi; "
            "if [ $i -eq 60 ]; then "
            'log "STEP 6: ComfyUI did NOT bind in 900s"; '
            "fi; "
            "sleep 15; "
            "done\n"
            "log 'STEP 7: copy ComfyUI log to /workspace/diag for visibility'\n"
            "tail -200 /workspace/diag/step5_comfy.log > /workspace/diag/comfy_tail.log 2>&1\n"
            "log 'STEP 8: keep diag server alive'\n"
            "exec sleep 1500\n"
        )
        from kinoforge.core.interfaces import RenderedProvision as _RP

        rendered = _RP(
            script=instrumented,
            run_cmd=["bash", "-c", "exec sleep 1500"],
            image=args.image_override or rendered.image,
            ports=["8188/http", "9000/http"],
            env_required=rendered.env_required,
        )

    print("--- rendered bootstrap (first 60 lines) ---")
    for ln in rendered.script.splitlines()[:60]:
        print(f"  | {ln}")
    print(
        f"--- rendered.ports={rendered.ports}  env_required={rendered.env_required} ---"
    )

    reqs = cfg.hardware_requirements()
    offers = provider.find_offers(reqs)
    if not offers:
        safe_print("diagnose_pod_boot: no offers")
        return 1

    # Build env from rendered.env_required via creds. After phonehome
    # override above, env_required is empty so this loop is a no-op.
    creds = EnvCredentialProvider()
    env: dict[str, str] = {}
    for var in rendered.env_required:
        val = creds.get(var)
        if val is None:
            safe_print(f"diagnose_pod_boot: missing cred {var}")
            return 1
        env[var] = val

    lifecycle = cfg.lifecycle()

    from kinoforge.core.errors import CapacityError

    instance = None
    chosen_offer = None
    for offer in offers:
        spec = InstanceSpec(
            image=rendered.image,
            offer=offer,
            ports=tuple(rendered.ports),
            volume_gb=cfg.compute.requirements.disk_gb or 50,
            volume_mount="/workspace",
            lifecycle=lifecycle,
            env=env,
            tags={"mode": "pod", "kinoforge_purpose": "diagnostic"},
            run_id="kinoforge-diagnose",
            provision_script=rendered.script,
            run_cmd=rendered.run_cmd,
        )
        try:
            print(f"\ntrying offer {offer.gpu_type} @ ${offer.cost_rate_usd_per_hr}/hr")
            instance = provider.create_instance(spec)
            chosen_offer = offer
            break
        except CapacityError as exc:
            print(f"  no capacity: {exc}")
            continue
        except ValueError as exc:
            # Generic RunPod errors ('Something went wrong', etc.) — log and skip.
            short = str(exc).splitlines()[0][:200]
            print(f"  rejected: {short}")
            continue
    if instance is None or chosen_offer is None:
        safe_print("diagnose_pod_boot: every offer rejected with CapacityError")
        return 1
    print(
        f"\ncreated pod {instance.id} on {chosen_offer.gpu_type} "
        f"@ ${chosen_offer.cost_rate_usd_per_hr}/hr — endpoints={instance.endpoints}"
    )

    diag_port = "9000" if args.instrument_production else "8188"
    proxy_root = f"https://{instance.id}-{diag_port}.proxy.runpod.net/"
    proxy_url = f"https://{instance.id}-{diag_port}.proxy.runpod.net/progress.log"
    comfy_proxy_url = f"https://{instance.id}-8188.proxy.runpod.net/system_stats"
    deadline = time.monotonic() + args.max_minutes * 60
    elapsed = 0
    try:
        while time.monotonic() < deadline:
            time.sleep(args.poll_interval_s)
            elapsed += args.poll_interval_s
            pod = _print_pod_snapshot(api_key, instance.id, elapsed)
            _print_pod_logs(api_key, instance.id)
            if args.minimal_phonehome or args.instrument_production:
                # First poll root: distinguishes "bash never ran" (root 404)
                # from "bash ran but no progress.log" (root 200, log 404).
                # Also probe ComfyUI's proxy URL directly so we see if it
                # routes correctly once ComfyUI binds.
                probe_targets = [("/", proxy_root), ("/progress.log", proxy_url)]
                if args.instrument_production:
                    probe_targets.append(("8188/system_stats", comfy_proxy_url))
                    base = f"https://{instance.id}-{diag_port}.proxy.runpod.net"
                    probe_targets.extend(
                        [
                            ("/step4_wan_pip.log", f"{base}/step4_wan_pip.log"),
                            (
                                "/step4b_kijai_probe.log",
                                f"{base}/step4b_kijai_probe.log",
                            ),
                            ("/step5_comfy.log", f"{base}/step5_comfy.log"),
                            ("/comfy_tail.log", f"{base}/comfy_tail.log"),
                        ]
                    )
                for label, url in probe_targets:
                    try:
                        req = urllib.request.Request(  # noqa: S310
                            url,
                            headers={"User-Agent": _UA},
                        )
                        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                            body = resp.read().decode("utf-8", errors="replace")
                        print(f"  GET {label} -> 200 ({len(body)} bytes)")
                        for ln in body.splitlines()[:20]:
                            print(f"    | {ln[:200]}")
                    except urllib.error.HTTPError as exc:
                        # Read body for the 404/5xx so we see if proxy or
                        # http.server returned it.
                        ebody = exc.read().decode("utf-8", errors="replace")[:200]
                        print(f"  GET {label} -> {exc.code}  body={ebody!r}")
                    except (urllib.error.URLError, OSError) as exc:
                        print(f"  GET {label} -> network err: {exc}")
            status = pod.get("desiredStatus") if pod else None
            if status in ("EXITED", "TERMINATED", "FAILED"):
                print(f"\npod entered terminal status {status!r} — stopping early")
                break
        return 0
    finally:
        print(f"\n--- DESTROY pod {instance.id} ---")
        code, body = _rest_call(
            f"https://rest.runpod.io/v1/pods/{instance.id}",
            api_key,
            method="DELETE",
        )
        print(f"  DELETE -> {code}")
        if code not in (200, 204):
            print(f"  {redact_string(body[:300])}")


if __name__ == "__main__":
    sys.exit(main())
