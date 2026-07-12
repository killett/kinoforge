"""Self-contained GPU/CPU/mem stats reader for the diffusers server /util route.

Runs INSIDE the pod/container, which has NO ``kinoforge.core`` — so this module
imports only stdlib + optional pynvml/psutil (both guarded). Returns a plain
dict; the controller-side ModalUtilEndpoint maps it to core's UtilSnapshot.

Source order for GPU util: pynvml (typed NVML) -> nvidia-smi subprocess -> None.
Every read is defensive: any failure degrades a field to None, never raises.
"""

from __future__ import annotations

import subprocess
import time

_START = time.monotonic()


def _read_gpu_via_pynvml() -> tuple[float, float] | None:
    """Return (gpu_util_percent, gpu_mem_percent) via NVML, or None on any failure."""
    try:
        import pynvml  # nvidia-ml-py

        pynvml.nvmlInit()
        try:
            n = pynvml.nvmlDeviceGetCount()
            best_util = 0.0
            best_mem = 0.0
            for i in range(n):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                rates = pynvml.nvmlDeviceGetUtilizationRates(h)
                best_util = max(best_util, float(rates.gpu))
                best_mem = max(best_mem, float(rates.memory))
            return (best_util, best_mem)
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None


def _read_gpu_via_smi() -> tuple[float, float] | None:
    """Return (gpu_util_percent, gpu_mem_util_percent) via nvidia-smi, or None."""
    try:
        out = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "nvidia-smi",
                "--query-gpu=utilization.gpu,utilization.memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        # One line per GPU: "util, mem" — take MAX util across devices.
        best_util = 0.0
        best_mem = 0.0
        for line in out.splitlines():
            u, m = (p.strip() for p in line.split(","))
            best_util = max(best_util, float(u))
            best_mem = max(best_mem, float(m))
        return (best_util, best_mem)
    except Exception:
        return None


def _read_host_via_psutil() -> tuple[float | None, float | None, float | None]:
    """Return (cpu_percent, memory_percent, disk_percent) via psutil, or Nones."""
    try:
        import psutil

        cpu = float(psutil.cpu_percent(interval=None))
        mem = float(psutil.virtual_memory().percent)
        disk = float(psutil.disk_usage("/").percent)
        return (cpu, mem, disk)
    except Exception:
        return (None, None, None)


def read_gpu_stats() -> dict[str, float | int | None]:
    """Return the five UtilSnapshot fields as a plain dict; never raises."""
    gpu = _read_gpu_via_pynvml() or _read_gpu_via_smi()
    gpu_util = gpu[0] if gpu is not None else None
    cpu, mem, disk = _read_host_via_psutil()
    return {
        "gpu_util_percent": gpu_util,
        "cpu_percent": cpu,
        "memory_percent": mem,
        "disk_percent": disk,
        "uptime_seconds": int(time.monotonic() - _START),
    }
