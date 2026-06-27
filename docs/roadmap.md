# Roadmap

(Moved from README §Roadmap (deferred layers and their seams) on 2026-06-27. See [../README.md](../README.md).)

## Roadmap (deferred layers and their seams)

Each item below names the deferred layer and the exact seam it plugs into when built:

- **Continuity / stitching fallback** — `strategy.decide` non-native branch; the fallback path currently issues N single-segment jobs; stitching post-processing slots in between `pool.map` and `store.put_bytes` in `GenerateClipStage`.
- **Audio sync layer** — `strategy.decide` sets `spec["_audio_mode"] = "separate"` as a marker; a downstream audio-sync stage reads this key and schedules audio generation after the video clip is stored.
- **Distributed / cross-process backend scheduler** — `ConcurrentPool` (Layer G) handles in-process thread-level concurrency; a future `RayPool` or cross-process variant would slot into the same `BackendPool` ABC without touching the stage or orchestrator.
- **Keyframe / image-generation upstream Stage** — `Stage` Protocol + `ConditioningAsset` with `kind="image"`; add an `ImageGenStage` that satisfies `Stage` and feeds its output into the video generation stage's `segments_override`.
- **Cross-process discovery lock** — `ModelProfileProvider` currently uses an in-process threading.Event for single-flight; replace with a file-lock or Redis-backed lock for multi-process / distributed workers.
