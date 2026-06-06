"""Layer W recording / replay infrastructure for boto3 + google-cloud-storage."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Literal


class FixtureMissError(LookupError):
    """Raised by replay mode when an incoming call has no matching fixture entry."""


def _git_sha() -> str:
    """Return the short HEAD git SHA."""
    return subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], text=True
    ).strip()


def _captured_at_local() -> str:
    """Return current local time as ISO-8601 string (never UTC)."""
    # Memory rule: local TZ, never UTC.
    return _dt.datetime.now().isoformat(timespec="seconds")


def _kinoforge_version() -> str:
    """Return kinoforge package version, or 'unknown' on failure."""
    try:
        from importlib.metadata import version

        return version("kinoforge")
    except Exception:
        return "unknown"


_REDACT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<AWS_ACCOUNT>"), "<AWS_ACCOUNT>"),
    (re.compile(r"<GCP_PROJECT>"), "<GCP_PROJECT>"),
    (
        re.compile(r"X-Amz-Signature=[^&\s\"]+", re.IGNORECASE),
        "X-Amz-Signature=<REDACTED>",
    ),
    (
        re.compile(r"X-Amz-Credential=[^&\s\"]+", re.IGNORECASE),
        "X-Amz-Credential=<REDACTED>",
    ),
    (
        re.compile(r"X-Goog-Signature=[^&\s\"]+", re.IGNORECASE),
        "X-Goog-Signature=<REDACTED>",
    ),
    (
        re.compile(r"x-goog-credential=[^&\s\"]+", re.IGNORECASE),
        "x-goog-credential=<REDACTED>",
    ),
]

_REDACT_HEADERS: frozenset[str] = frozenset(
    {"authorization", "x-amz-security-token", "x-goog-authorization"}
)


def _drop_secret_headers(obj: Any) -> Any:
    """Recursively remove any dict key whose lower-case form is a redacted header."""
    if isinstance(obj, dict):
        return {
            k: _drop_secret_headers(v)
            for k, v in obj.items()
            if k.lower() not in _REDACT_HEADERS
        }
    if isinstance(obj, list):
        return [_drop_secret_headers(v) for v in obj]
    return obj


def _redact(payload: Any, extra_subs: dict[str, str] | None = None) -> Any:
    """Recursively redact secrets from a JSON-shaped payload.

    Args:
        payload: Any JSON-serialisable structure (dict, list, str, …).
        extra_subs: Additional literal-string substitutions, e.g.
            ``{"arn:aws:kms:…": "<S3_KMS_KEY>"}``. Applied after the
            built-in regex rules.

    Returns:
        A new structure with secrets replaced.
    """
    subs = dict(extra_subs or {})
    text = json.dumps(payload)
    for pattern, replacement in _REDACT_RULES:
        text = pattern.sub(replacement, text)
    for needle, replacement in subs.items():
        text = text.replace(needle, replacement)
    out: Any = json.loads(text)
    return _drop_secret_headers(out)


def _persist(
    label: str,
    payload: dict[str, Any],
    target_path: Path,
    *,
    cloud: str,
    axis: str,
    extra_subs: dict[str, str] | None = None,
) -> None:
    """Write a redacted fixture JSON file to *target_path*.

    Args:
        label: Human-readable label stored in ``_meta.label``.
        payload: The raw captured payload (will be redacted before writing).
        target_path: Destination ``.json`` file path (created with parents).
        cloud: ``"s3"`` or ``"gcs"``.
        axis: Test axis name, e.g. ``"hot_path"``.
        extra_subs: Passed through to :func:`_redact`.
    """
    body: dict[str, Any] = {
        "_meta": {
            "git_sha": _git_sha(),
            "captured_at_local": _captured_at_local(),
            "kinoforge_version": _kinoforge_version(),
            "cloud": cloud,
            "axis": axis,
            "label": label,
        },
        "entries": _redact(payload, extra_subs=extra_subs),
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(body, indent=2, sort_keys=True))


def _body_hash(body: bytes | None) -> str:
    """Return a hex-digest hash of *body*, or empty string for None."""
    if body is None:
        return ""
    return hashlib.sha256(body).hexdigest()


# ----------------------------------------------------------------------------
# S3 recorder — botocore event hooks.
# ----------------------------------------------------------------------------


class S3Recorder:
    """Record or replay S3 wire traffic via botocore event hooks.

    Args:
        mode: ``"record"`` captures live responses; ``"replay"`` short-circuits
            botocore and returns fixture data.
        fixture_path: Required (and read eagerly) when *mode* is ``"replay"``.
    """

    def __init__(
        self,
        mode: Literal["record", "replay"],
        *,
        fixture_path: Path | None = None,
    ) -> None:
        self.mode = mode
        self.fixture_path = fixture_path
        self.captured: list[dict[str, Any]] = []
        if mode == "replay":
            assert fixture_path is not None, "fixture_path required in replay mode"
            self._fixture: list[dict[str, Any]] = json.loads(fixture_path.read_text())[
                "entries"
            ]
        else:
            self._fixture = []

    def attach(self, session: Any) -> None:
        """Register botocore event handlers on *session*.

        Args:
            session: A ``boto3.Session`` instance.
        """
        events = session.events
        events.register("before-send.s3.*", self._before_send)
        events.register("after-call.s3.*", self._after_call)

    def _match_key(self, operation: str, params: dict[str, Any]) -> str:
        """Derive a stable match key from operation name + params."""
        digest = hashlib.sha256(
            json.dumps(params, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        return f"{operation}:{digest}"

    def _before_send(self, **kwargs: Any) -> Any:
        """botocore ``before-send`` handler.

        In **record** mode: stashes the operation name + params on the request
        context so ``_after_call`` can pair them, then returns ``None`` (lets the
        real request proceed).

        In **replay** mode: returns a ``(status_code, headers, body)`` tuple to
        short-circuit the actual HTTP call; raises :exc:`FixtureMissError` when no
        matching fixture entry exists.
        """
        request = kwargs.get("request")
        op = kwargs.get("operation_name", "")
        params: dict[str, Any] = kwargs.get("params", {})

        # Always stash on context so after-call can pair them (record mode).
        if request is not None and hasattr(request, "context"):
            request.context["_kinoforge_op"] = op
            request.context["_kinoforge_params"] = params

        if self.mode == "replay":
            key = self._match_key(op, params)
            for entry in self._fixture:
                if entry["match_key"] == key:
                    return entry["parsed_response_http_form"]
            raise FixtureMissError(
                f"No fixture entry for operation={op!r} params_hash={key!r}"
            )
        # record mode — let botocore proceed normally
        return None

    def _after_call(
        self,
        http_response: Any,
        parsed: Any,
        model: Any,
        context: Any,
        **kwargs: Any,
    ) -> None:
        """botocore ``after-call`` handler — captures the parsed response."""
        if self.mode != "record":
            return
        op = context.get("_kinoforge_op", "") if isinstance(context, dict) else ""
        params: dict[str, Any] = (
            context.get("_kinoforge_params", {}) if isinstance(context, dict) else {}
        )
        self.captured.append(
            {
                "operation": op,
                "params": params,
                "match_key": self._match_key(op, params),
                "parsed_response": parsed,
                # HTTP-form stored as [status, headers, body_b64] so the whole
                # structure is JSON-serialisable.  _before_send reconstructs the
                # tuple from these three fields on replay.
                "parsed_response_http_form": [
                    http_response.status_code,
                    dict(http_response.headers),
                    base64.b64encode(http_response.content).decode("ascii"),
                ],
            }
        )

    def flush(
        self,
        target_path: Path,
        *,
        axis: str,
        extra_subs: dict[str, str] | None = None,
    ) -> None:
        """Redact and write captured entries to *target_path*.

        Args:
            target_path: Destination JSON file.
            axis: Test axis label stored in ``_meta``.
            extra_subs: Extra literal substitutions (e.g. KMS ARN → placeholder).
        """
        assert self.mode == "record", "flush() only valid in record mode"
        _persist(
            label=axis,
            payload={"entries": self.captured},
            target_path=target_path,
            cloud="s3",
            axis=axis,
            extra_subs=extra_subs,
        )


# ----------------------------------------------------------------------------
# GCS recorder — requests.adapters.HTTPAdapter subclass.
# ----------------------------------------------------------------------------


class _GCSRecordingAdapter:
    """Wraps an existing ``requests`` adapter to record or replay HTTPS round-trips.

    Args:
        recorder: The :class:`GCSRecorder` that owns this adapter.
        inner_adapter: The original ``HTTPAdapter`` replaced by this wrapper.
    """

    def __init__(self, recorder: GCSRecorder, inner_adapter: Any) -> None:
        self.recorder = recorder
        self.inner = inner_adapter

    def send(self, request: Any, **kwargs: Any) -> Any:
        """Intercept a ``PreparedRequest``-like object.

        Args:
            request: Object with ``.method``, ``.url``, ``.body``, ``.headers``.
            **kwargs: Forwarded to the inner adapter in record mode.

        Returns:
            A ``requests.Response`` (real or reconstructed from fixture).

        Raises:
            FixtureMissError: In replay mode when no fixture entry matches.
        """
        # Normalise body to bytes for hashing.
        raw_body = request.body
        if isinstance(raw_body, (bytes, bytearray)):
            body_bytes: bytes | None = bytes(raw_body)
        elif isinstance(raw_body, str):
            body_bytes = raw_body.encode()
        else:
            body_bytes = None

        key = self.recorder._match_key(request.method, request.url, body_bytes)

        if self.recorder.mode == "replay":
            for entry in self.recorder._fixture:
                if entry["match_key"] == key:
                    import requests as _requests

                    resp = _requests.Response()
                    resp.status_code = entry["status"]
                    resp.headers.update(entry["headers"])
                    resp._content = base64.b64decode(entry["body_b64"])
                    return resp
            raise FixtureMissError(
                f"No fixture entry for {request.method} {request.url}"
            )

        # record mode — forward and capture
        resp = self.inner.send(request, **kwargs)
        self.recorder._record_response(request, body_bytes, resp)
        return resp

    def close(self) -> None:
        """Delegate close to the inner adapter."""
        self.inner.close()


class GCSRecorder:
    """Record or replay GCS HTTPS traffic via a custom requests adapter.

    Args:
        mode: ``"record"`` or ``"replay"``.
        fixture_path: Required when *mode* is ``"replay"``.
    """

    def __init__(
        self,
        mode: Literal["record", "replay"],
        *,
        fixture_path: Path | None = None,
    ) -> None:
        self.mode = mode
        self.fixture_path = fixture_path
        self.captured: list[dict[str, Any]] = []
        if mode == "replay":
            assert fixture_path is not None, "fixture_path required in replay mode"
            self._fixture: list[dict[str, Any]] = json.loads(fixture_path.read_text())[
                "entries"
            ]
        else:
            self._fixture = []

    def attach(self, session: Any) -> None:
        """Mount the recording adapter on *session* for ``storage.googleapis.com``.

        Args:
            session: An ``AuthorizedSession`` (``google.auth.transport.requests``)
                or any ``requests.Session`` — whatever ``storage.Client._http`` is.
        """
        existing = session.get_adapter("https://")
        adapter = _GCSRecordingAdapter(self, existing)
        session.mount("https://storage.googleapis.com/", adapter)

    def _match_key(self, method: str, url: str, body: bytes | None) -> str:
        """Derive a stable match key from method + URL + body hash."""
        return f"{method}:{url}:{_body_hash(body)[:16]}"

    def _record_response(self, request: Any, body: bytes | None, response: Any) -> None:
        """Append one captured interaction to ``self.captured``."""
        self.captured.append(
            {
                "method": request.method,
                "url": request.url,
                "body_hash": _body_hash(body),
                "match_key": self._match_key(request.method, request.url, body),
                "status": response.status_code,
                "headers": dict(response.headers),
                "body_b64": base64.b64encode(response.content).decode("ascii"),
            }
        )

    def flush(
        self,
        target_path: Path,
        *,
        axis: str,
        extra_subs: dict[str, str] | None = None,
    ) -> None:
        """Redact and write captured entries to *target_path*.

        Args:
            target_path: Destination JSON file.
            axis: Test axis label stored in ``_meta``.
            extra_subs: Extra literal substitutions (e.g. KMS key name → placeholder).
        """
        _persist(
            label=axis,
            payload={"entries": self.captured},
            target_path=target_path,
            cloud="gcs",
            axis=axis,
            extra_subs=extra_subs,
        )


# ----------------------------------------------------------------------------
# Fixture-replay clients exposed for offline tests.
# ----------------------------------------------------------------------------


class FixtureReplayS3Client:
    """Minimal boto3 S3 client surface backed by an :class:`S3Recorder` in replay mode.

    Note:
        Task 11 fleshes out the full implementation.

    Args:
        fixture_path: Path to a previously captured fixture JSON file.

    Raises:
        NotImplementedError: Always — Task 11 provides the real implementation.
    """

    def __init__(self, fixture_path: Path) -> None:
        self._recorder = S3Recorder(mode="replay", fixture_path=fixture_path)
        raise NotImplementedError("Layer W T11 fleshes this out")


class FixtureReplayGCSClient:
    """Minimal GCS client surface backed by a :class:`GCSRecorder` in replay mode.

    Note:
        Task 11 fleshes out the full implementation.

    Args:
        fixture_path: Path to a previously captured fixture JSON file.

    Raises:
        NotImplementedError: Always — Task 11 provides the real implementation.
    """

    def __init__(self, fixture_path: Path) -> None:
        self._recorder = GCSRecorder(mode="replay", fixture_path=fixture_path)
        raise NotImplementedError("Layer W T11 fleshes this out")
