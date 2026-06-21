"""Shared smoke-test harness for kinoforge LoRA-swap tiers.

Centralises the four kinoforge-internal HTTP patterns rediscovered
four separate times during the 2026-06-20 T22 smoke attempts
($2.15 burned). Future engine smokes (C23 ComfyUI, Wan 3.0, Flux)
inherit them by import, not by rediscovery.

Patterns:
  1. ``User-Agent: kinoforge-smoke/0.1`` — Cloudflare gate dodge.
  2. ``?api_key=<RUNPOD_API_KEY>`` URL suffix — RunPod proxy auth.
  3. ``urllib.error.URLError`` retry budget — RunPod GraphQL transient.
  4. Belt-and-suspenders ``destroy_all_active_pods`` sweep in finally.

Spec: docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md.
"""
