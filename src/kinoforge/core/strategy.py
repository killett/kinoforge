"""Long-video strategy decision point.

Pure function: given a model's capability profile + an ordered segment list,
produce the appropriate GenerationJob shape — one N-segment job for native-
extension engines, or N single-segment jobs for the stitching fallback path.
The fallback's stitching/continuity logic itself is DEFERRED — this module
only packages segments; downstream code handles the rest.
"""

from __future__ import annotations

from dataclasses import replace

from kinoforge.core.interfaces import GenerationJob, ModelProfile, Segment


def _merged_segment(base_params: dict, segment: Segment) -> Segment:  # type: ignore[type-arg]
    """Return a copy of *segment* whose params merge base under segment-wins.

    Args:
        base_params: Engine-neutral defaults shared across all segments.
        segment: The input segment whose params take priority.

    Returns:
        A new Segment with params = {**base_params, **segment.params}.
    """
    merged = {**base_params, **segment.params}
    return replace(segment, params=merged)


def decide(
    profile: ModelProfile,
    segments: list[Segment],
    params: dict,  # type: ignore[type-arg]
    spec: dict,  # type: ignore[type-arg]
) -> list[GenerationJob]:
    """Package segments into GenerationJob(s) per the model's strategy flags.

    Args:
        profile: The model's capability profile (drives both decisions).
        segments: Ordered list of clip-sized segments produced upstream.
        params: Engine-neutral defaults shared across segments. The produced
            ``GenerationJob.params`` is this dict; per-segment params are
            merged segment-wins onto each produced Segment.
        spec: Engine-interpreted payload. The decision point adds an
            ``_audio_mode`` marker so a downstream stage can branch on
            joint vs separate audio without re-inspecting the profile.

    Returns:
        Either a single-element list (native extension) carrying all
        segments, or one job per segment (stitching fallback). Segment
        order and assets are preserved in both branches; per-segment
        ``params`` carry the segment-wins merge over the base ``params``.
    """
    audio_mode = "joint" if profile.supports_joint_audio else "separate"
    job_spec = {**spec, "_audio_mode": audio_mode}
    merged_segments = [_merged_segment(params, s) for s in segments]

    if profile.supports_native_extension:
        return [
            GenerationJob(spec=job_spec, segments=merged_segments, params=dict(params))
        ]

    return [
        GenerationJob(spec=job_spec, segments=[s], params=dict(params))
        for s in merged_segments
    ]
