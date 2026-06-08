"""LocalOutputSink — default local-filesystem publish sink.

Writes bytes to ``<dir>/<namespace>/<ts>_<slug><ext>`` with atomic
rename semantics and ``_2.._99`` then sha256-hash collision suffixes.
Self-registers under ``"local"`` on import.

Layer P deferred: hardlink optimization via
``ArtifactStore.local_path_for(run_id, name)``.  V1 always writes bytes,
doubling local-store disk usage for one clip — negligible for sub-GB mp4s.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime
from pathlib import Path

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.logging import get_logger
from kinoforge.outputs import register_sink
from kinoforge.outputs.base import OutputPublishError, format_filename, slugify

_log = get_logger(__name__)

_MAX_COLLISION_SUFFIX = 99
_MAX_HASH_RETRIES = 16


class LocalOutputSink:
    """Publish bytes to a local-filesystem directory with friendly filenames.

    Attributes:
        dir: Absolute directory root for all publishes (resolved at
            construction).
        clock: Time source — usually ``RealClock``; tests inject ``FakeClock``.
    """

    def __init__(self, dir: Path, clock: Clock | None = None) -> None:
        """Initialise the sink with a destination directory and optional clock.

        Args:
            dir: Destination root; relative paths are resolved against cwd
                at construction time.
            clock: Time source; defaults to :class:`RealClock`.
        """
        self.dir = Path(dir).resolve()
        self.clock = clock or RealClock()

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """Write *data* to ``<dir>/<namespace?>/<ts>_<provider>_<model>_<slug><ext>``.

        Args:
            data: Raw bytes to write.
            prompt: Source prompt; first 20 ASCII chars become the slug.
            extension: File suffix with the dot (e.g. ``".mp4"``); empty
                string defaults to ``".bin"``.
            namespace: Optional batch_id subdirectory.
            provider: Engine registry key; ``None`` / empty → ``"unknown"``.
            model: Slugified model identifier; ``None`` / empty → ``"unknown"``.

        Returns:
            Absolute path of the published file as a string.

        Raises:
            OutputPublishError: The write or the atomic replace failed.
        """
        ext = extension or ".bin"
        ts = datetime.fromtimestamp(self.clock.now()).strftime("%Y%m%d-%H%M%S")
        slug = slugify(prompt)
        provider_slug = slugify(provider, max_chars=20) if provider else ""
        model_slug = slugify(model, max_chars=24) if model else ""
        # `slugify` returns the literal "clip" when the input is empty, so we
        # treat that as "no value supplied" too.
        if not provider_slug or provider_slug == "clip":
            provider_slug = "unknown"
        if not model_slug or model_slug == "clip":
            model_slug = "unknown"

        target_dir = self.dir / namespace if namespace else self.dir
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputPublishError(
                f"failed to create output directory {target_dir}: {exc}"
            ) from exc

        path = self._resolve_collision(
            target_dir, ts, provider_slug, model_slug, slug, ext
        )

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, path)
        except OSError as exc:
            # Cleanup the partial file so an operator never sees a half-byte mp4.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OutputPublishError(f"failed to publish to {path}: {exc}") from exc

        _log.info("output published: %s", path)
        return str(path)

    def _resolve_collision(
        self,
        target_dir: Path,
        ts: str,
        provider: str,
        model: str,
        slug: str,
        ext: str,
    ) -> Path:
        """Return the first non-existing path in the collision sequence.

        Sequence: ``base.ext`` → ``base_2.ext`` → ... → ``base_99.ext`` →
        ``base_<6-char-sha256>.ext``, where ``base`` is
        ``{ts}_{provider}_{model}_{slug}``.

        Args:
            target_dir: Directory in which to check for collisions.
            ts: Timestamp string portion of the filename.
            provider: Pre-slugified provider name.
            model: Pre-slugified model identifier.
            slug: Slug portion of the filename.
            ext: File extension including the dot.

        Returns:
            A :class:`~pathlib.Path` that does not currently exist.
        """
        base = f"{ts}_{provider}_{model}_{slug}"
        primary = target_dir / format_filename(
            ts=ts, provider=provider, model=model, slug=slug, extension=ext
        )
        if not primary.exists():
            return primary

        for n in range(2, _MAX_COLLISION_SUFFIX + 1):
            candidate = target_dir / f"{base}_{n}{ext}"
            if not candidate.exists():
                return candidate

        # 99 exhausted — fall back to up-to-_MAX_HASH_RETRIES attempts at a
        # 6-char sha256 hash of monotonic_ns().  Each retry advances the
        # nanosecond clock so the hash changes; failure to find a free slot
        # after _MAX_HASH_RETRIES is a genuine pathological state (extreme
        # collision rate or filesystem corruption) and we raise so the
        # Protocol's "must never silently overwrite" contract holds.
        for _ in range(_MAX_HASH_RETRIES):
            hash_suffix = hashlib.sha256(str(time.monotonic_ns()).encode()).hexdigest()[
                :6
            ]
            candidate = target_dir / f"{base}_{hash_suffix}{ext}"
            if not candidate.exists():
                return candidate
        raise OutputPublishError(
            f"could not find a non-colliding output path in {target_dir} "
            f"after {_MAX_COLLISION_SUFFIX - 1} numeric and {_MAX_HASH_RETRIES} hash attempts"
        )


def _factory() -> LocalOutputSink:
    """Zero-arg factory used by the registry.

    Real configuration of the sink (dir / clock) is done by
    ``cli._build_sink`` which constructs the class directly; this factory
    just exists so ``get_sink("local")`` doesn't raise UnknownAdapter for
    callers that want a default-rooted sink at cwd.

    Returns:
        A :class:`LocalOutputSink` rooted at ``cwd/output``.
    """
    return LocalOutputSink(dir=Path.cwd() / "output")


register_sink("local", _factory)
