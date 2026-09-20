# Roadmap

(Moved from README §Roadmap (deferred layers and their seams) on 2026-06-27. See [../README.md](../README.md).)

## Roadmap (deferred layers and their seams)

Each item below names the deferred layer and the exact seam it plugs into when built:

- **Continuity / stitching fallback** — `strategy.decide`'s non-native branch still issues N single-segment jobs, and `GenerateClipStage` fans them out through `pool.map` with no join; stitching post-processing slots in between `pool.map` and `store.put_bytes`. The frame-level primitives now exist — `pipeline/tile.py:stitch_frames` feather-blends tiles for upscaling — but nothing wires them to the generate fallback.
- **Separate audio-sync layer** — *partly superseded.* Joint audio shipped by another route: MiniMax-H3's `t2va` mode emits a soundtrack with the video, muxed on the pod by `engines/diffusers/servers/_av_io.write_mp4_with_audio`. The originally planned path — generate audio separately and sync it after the clip is stored — remains unbuilt. `strategy.decide` still writes a `spec["_audio_mode"]` marker that the controller deliberately leaves inert (`core/strategy.py:decide`); a downstream audio-sync stage would read that key.
- **Distributed / cross-process backend scheduler** — `ConcurrentPool` (Layer G, `core/pool.py:ConcurrentPool`) handles in-process thread-level concurrency; a future `RayPool` or cross-process variant would slot into the same `BackendPool` ABC (`core/interfaces.py:BackendPool`) without touching the stage or orchestrator.
- **Cross-process discovery lock** — `JsonProfileCache` (`core/profiles.py:JsonProfileCache`) uses an in-process `dict[str, threading.Event]` for per-key single-flight; multi-process or distributed workers need a file-lock or Redis-backed lock instead. `stores/local_lock.FileLock` already implements the leased-file primitive.
- **SeedVR2 vendoring (Phase 2)** — `upscalers/seedvr2/` self-registers and its config parses, but the four heavyweight methods raise `ExtrasNotInstalled`. Upstream `ByteDance-Seed/SeedVR` ships research scripts with no packaging, so `pip install seedvr @ git+...` is not feasible; the seam is a vendored copy of `projects/inference_seedvr2_*.py` + `common/` + `models/` under `upscalers/seedvr2/_vendored/`.

Deferred on an external blocker rather than an internal seam:

- **Azure via SkyPilot** — SkyPilot's `[azure]` extra pulls `azure-cli`, which pins `azure-batch` to a pre-release-only range: conda-forge jumps 14.2.0 → 15.1.0 with no 15.0.x build, and pixi exposes no prerelease allowlist. Revisit when conda-forge ships `azure-batch 15.0.x` GA, `azure-cli` loosens the pin, or pixi gains the toggle (the TODO in `pixi.toml` carries the full condition).
- **Vast.ai via SkyPilot** — the config and design exist (`examples/configs/skypilot-vast-diffusers-flashvsr-upscale.yaml`), but sky's vast adapter reaches for an attribute that `vastai-sdk` >= 0.2 no longer exposes, so the launch AttributeErrors. Blocked until the upstream fix lands; Lambda is the working sky cloud meanwhile.

Shipped since this list was written, and removed from it: the keyframe / image-generation upstream stage, now `pipeline/keyframe.py:KeyframeStage` feeding `ConditioningAsset`s into the video stage.
