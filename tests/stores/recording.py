"""Layer W recording / replay infrastructure for boto3 + google-cloud-storage."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
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


class _SafeEncoder(json.JSONEncoder):
    """JSON encoder that handles types boto3 / GCS return in parsed responses.

    Handles:
    - ``datetime`` / ``date`` → ISO-8601 string
    - ``bytes`` → base64 string
    - Unknown types → ``repr()`` string (never raises)
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, (_dt.datetime, _dt.date)):
            return o.isoformat()
        if isinstance(o, (bytes, bytearray)):
            return base64.b64encode(o).decode("ascii")
        try:
            return super().default(o)
        except TypeError:
            return repr(o)


def _redact(payload: Any, extra_subs: dict[str, str] | None = None) -> Any:
    """Recursively redact secrets from a JSON-shaped payload.

    Args:
        payload: Any JSON-serialisable structure (dict, list, str, …).
            Non-serialisable values (e.g. ``datetime``, ``bytes``) are
            coerced to strings by :class:`_SafeEncoder`.
        extra_subs: Additional literal-string substitutions, e.g.
            ``{"arn:aws:kms:…": "<S3_KMS_KEY>"}``. Applied BEFORE the
            built-in regex rules so full identifiers (e.g. KMS ARNs with
            account IDs embedded) match before account-id regex mutates them.

    Returns:
        A new structure with secrets replaced.
    """
    subs = dict(extra_subs or {})
    text = json.dumps(payload, cls=_SafeEncoder)
    # extra_subs FIRST so full identifiers (KMS ARNs, project paths) match before regex
    # rules mutate sub-segments (account ids, project ids).
    for needle, replacement in subs.items():
        text = text.replace(needle, replacement)
    for pattern, replacement in _REDACT_RULES:
        text = pattern.sub(replacement, text)
    out: Any = json.loads(text)
    return _drop_secret_headers(out)


def _persist(
    label: str,
    payload: list[dict[str, Any]],
    target_path: Path,
    *,
    cloud: str,
    axis: str,
    extra_subs: dict[str, str] | None = None,
) -> None:
    """Write a redacted fixture JSON file to *target_path*.

    The on-disk shape is always::

        {"_meta": {...}, "entries": [<entry>, <entry>, ...]}

    Args:
        label: Human-readable label stored in ``_meta.label``.
        payload: The raw list of captured entry dicts (will be redacted before
            writing).  Must be a **list**, not a wrapper dict.
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
# AWSResponse-shaped replay object.
# ----------------------------------------------------------------------------


class _ReplayResponse:
    """AWSResponse-shaped object returned by :class:`S3Recorder` replay short-circuit.

    botocore's ``before-send`` short-circuit must return an object exposing
    ``.status_code``, ``.headers``, and ``.content`` — the same attributes
    present on ``botocore.awsrequest.AWSResponse``.  We hand-roll this to avoid
    coupling to botocore internals that may drift across versions.

    Args:
        status_code: HTTP status code, e.g. ``200``.
        headers: Response headers dict.
        content: Raw response body bytes.
    """

    def __init__(
        self, status_code: int, headers: dict[str, str], content: bytes
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content


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
                    raw = entry["parsed_response_http_form"]
                    # raw is [status_code, headers_dict, body_b64]
                    status_code: int = raw[0]
                    headers: dict[str, str] = raw[1]
                    content: bytes = base64.b64decode(raw[2])
                    return _ReplayResponse(status_code, headers, content)
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
        # TODO(Layer W follow-up): botocore does NOT propagate the context dict set in
        # ``before-send`` to the ``after-call`` event, so ``_kinoforge_op`` is always
        # missing here and ``op`` is always ``''``.  The workaround is
        # ``_s3_op_fingerprint`` (this file, same module), which classifies entries by
        # request-params shape instead of by operation name.  To fix the root cause,
        # research whether a botocore ``before-call`` hook (fired with the full context
        # before the request is built) can inject the op name more reliably, or whether
        # ``provide-client-params.s3.*`` is a better attachment point.
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
                    # Use _content (already-cached) to avoid consuming a
                    # streaming body (e.g. GetObject).  If not yet cached
                    # (streaming response), store empty bytes — the parsed
                    # response carries the data for replay.
                    base64.b64encode(
                        http_response._content
                        if getattr(http_response, "_content", None) is not None
                        else b""
                    ).decode("ascii"),
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
            payload=self.captured,
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
        elif hasattr(raw_body, "read"):
            body_bytes = raw_body.read()
            # Reset stream position so callers can re-read if needed.
            if hasattr(raw_body, "seek"):
                try:
                    raw_body.seek(0)
                except Exception:
                    pass
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
            payload=self.captured,
            target_path=target_path,
            cloud="gcs",
            axis=axis,
            extra_subs=extra_subs,
        )


# ----------------------------------------------------------------------------
# Fixture-replay clients exposed for offline tests.
# ----------------------------------------------------------------------------


def _s3_op_fingerprint(entry: dict[str, Any]) -> str:
    """Classify an S3 fixture entry by its request params and parsed-response shape.

    The live captures have ``operation=''`` (botocore context was not
    propagated), so we infer the operation from distinctive params/response keys.

    UploadPart is detected first via the **request params** (``PartNumber``
    present), not by the parsed-response shape.  Both ``UploadPart`` and
    ``PutObject`` responses carry ``ETag + ChecksumCRC32 + ResponseMetadata``
    when checksum validation is enabled in botocore; only ``UploadPart``
    requests carry ``PartNumber`` in the request params.

    Args:
        entry: A single fixture entry dict.

    Returns:
        A string operation label, e.g. ``"HeadObject"``.
    """
    # Pivot on request params FIRST to disambiguate before inspecting response shape.
    params: dict[str, Any] = entry.get("params", {})
    if "PartNumber" in params:
        return "UploadPart"

    keys: frozenset[str] = frozenset(entry["parsed_response"].keys())
    if "UploadId" in keys:
        return "CreateMultipartUpload"
    if "Body" in keys:
        return "GetObject"
    if "Contents" in keys or "KeyCount" in keys:
        return "ListObjectsV2"
    if "Location" in keys:
        return "CompleteMultipartUpload"
    if "AcceptRanges" in keys:
        return "HeadObject"
    if keys == frozenset({"ResponseMetadata"}):
        return "DeleteObject"
    # Fallback — put_object / upload_fileobj acknowledgement.
    if "ETag" in keys:
        return "PutObject"
    return "Unknown"


class _FixtureReplayS3Exceptions:
    """Stand-in ``exceptions`` namespace for :class:`FixtureReplayS3Client`."""

    class NoSuchKey(Exception):
        """Mimic boto3 NoSuchKey."""

    class ClientError(Exception):
        """Mimic botocore ClientError."""

        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.response = {"Error": {"Code": code}}


class FixtureReplayS3Client:
    """boto3 S3 client surface backed by pre-captured fixture JSON.

    The live-capture recorder does not preserve operation names (they arrive
    as empty strings due to a botocore-context gap).  This client therefore
    classifies each fixture entry by **response-shape fingerprint** rather
    than by match-key lookup, and returns entries in first-match order.

    Args:
        fixture_path: Path to a previously captured ``.json`` fixture file
            (shape: ``{"_meta": {...}, "entries": [...]}``.
    """

    exceptions = _FixtureReplayS3Exceptions()

    def __init__(self, fixture_path: Path) -> None:
        raw = json.loads(Path(fixture_path).read_text())
        self._entries: list[dict[str, Any]] = raw["entries"]
        self.meta = SimpleNamespace(
            config=SimpleNamespace(
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def set_retry_config(self, retries: dict[str, Any]) -> None:
        """Mirror the S3ArtifactStore retry-config setter.

        Args:
            retries: Dict with ``max_attempts`` and ``mode`` keys.
        """
        self.meta.config.retries = retries

    def _first_by_op(self, op: str) -> dict[str, Any]:
        """Return the parsed_response of the first entry classified as *op*.

        Args:
            op: Operation label as returned by :func:`_s3_op_fingerprint`.

        Returns:
            The ``parsed_response`` dict from the matching entry.

        Raises:
            FixtureMissError: When no entry matches.
        """
        for entry in self._entries:
            if _s3_op_fingerprint(entry) == op:
                return dict(entry["parsed_response"])
        raise FixtureMissError(f"No fixture entry classified as {op!r}")

    # ------------------------------------------------------------------
    # Public SDK surface
    # ------------------------------------------------------------------

    def upload_fileobj(
        self,
        fileobj: Any,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
        ExtraArgs: dict[str, Any] | None = None,  # noqa: N803
    ) -> None:
        """Replay an upload_fileobj call — silently acknowledged (no return value).

        The fixture may carry a PutObject or UploadPart response, but the
        production ``S3ArtifactStore`` does not inspect the return value of
        ``upload_fileobj``, so we return ``None``.

        Args:
            fileobj: Ignored in replay mode.
            Bucket: Ignored in replay mode.
            Key: Ignored in replay mode.
            ExtraArgs: Ignored in replay mode.
        """

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        """Return the recorded HeadObject response.

        Args:
            Bucket: Ignored — fixture is keyed by response shape.
            Key: Ignored — fixture is keyed by response shape.

        Returns:
            Parsed HeadObject response dict.
        """
        return self._first_by_op("HeadObject")

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        """Return the recorded GetObject response with a reconstructed Body.

        The ``Body`` value is replaced with an in-memory ``BytesIO`` so callers
        can call ``.read()`` without botocore internals.

        Args:
            Bucket: Ignored.
            Key: Ignored.

        Returns:
            Parsed GetObject response dict with ``Body`` replaced by BytesIO.
        """
        resp = self._first_by_op("GetObject")
        # The recorded Body is a repr-string ("botocore.response.StreamingBody ...").
        # Replace it with an empty BytesIO so callers can .read() it.
        resp["Body"] = io.BytesIO(b"")
        return resp

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "") -> dict[str, Any]:
        """Return the recorded ListObjectsV2 response.

        Args:
            Bucket: Ignored.
            Prefix: Ignored.

        Returns:
            Parsed ListObjectsV2 response dict.
        """
        return self._first_by_op("ListObjectsV2")

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        """Return the recorded DeleteObject response.

        Args:
            Bucket: Ignored.
            Key: Ignored.

        Returns:
            Parsed DeleteObject response dict (typically just ``ResponseMetadata``).
        """
        return self._first_by_op("DeleteObject")

    def generate_presigned_url(
        self,
        op: str,
        *,
        Params: dict[str, Any],  # noqa: N803
        ExpiresIn: int,  # noqa: N803
    ) -> str:
        """Return a synthetic presigned URL reconstructed from fixture metadata.

        Real ``generate_presigned_url`` is SDK-local (no wire call), so the
        fixture contains no dedicated entry for it.  We synthesise a URL that
        satisfies the wire-shape invariants the offline tests assert: starts
        with ``https://`` and embeds the bucket name.

        Args:
            op: ``"get_object"`` or ``"put_object"``.
            Params: Must contain at least ``Bucket`` and ``Key``.
            ExpiresIn: TTL in seconds.

        Returns:
            A synthetic HTTPS presigned URL string.
        """
        bucket = Params.get("Bucket", "<bucket>")
        key = Params.get("Key", "<key>")
        return (
            f"https://{bucket}.s3.amazonaws.com/{key}"
            f"?X-Amz-Signature=<REDACTED>&X-Amz-Expires={ExpiresIn}"
        )

    def get_paginator(self, op: str) -> _FixtureReplayS3Paginator:
        """Return a paginator backed by the ListObjectsV2 fixture entry.

        Args:
            op: Must be ``"list_objects_v2"``.

        Returns:
            A :class:`_FixtureReplayS3Paginator` instance.
        """
        assert op == "list_objects_v2", f"unexpected paginator op: {op!r}"
        return _FixtureReplayS3Paginator(self._first_by_op("ListObjectsV2"))


class _FixtureReplayS3Paginator:
    """Single-page paginator backed by a ListObjectsV2 fixture entry.

    Args:
        list_response: Parsed ListObjectsV2 response dict.
    """

    def __init__(self, list_response: dict[str, Any]) -> None:
        self._response = list_response

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        """Yield the single fixture page.

        Args:
            Bucket: Ignored.
            Prefix: Ignored.

        Returns:
            List containing the single fixture response.
        """
        return [self._response]


# ---------------------------------------------------------------------------
# GCS fixture replay — HTTP-level fixture → high-level blob surface.
# ---------------------------------------------------------------------------


class _FixtureReplayGCSBlob:
    """A fixture-backed blob exposing the surface GCSArtifactStore calls.

    Args:
        name: Blob name (key).
        metadata: The parsed JSON metadata dict from the fixture PUT response,
            or ``None`` if the blob only has raw bytes (e.g. download response).
        content: Raw bytes body (from the GET response), or ``b""`` if absent.
    """

    def __init__(
        self,
        name: str,
        metadata: dict[str, Any] | None,
        content: bytes,
    ) -> None:
        self.name = name
        self._metadata = metadata or {}
        self._content = content
        self.kms_key_name: str | None = self._metadata.get("kmsKeyName")
        self.size: int = int(self._metadata.get("size", len(content)))

    def upload_from_file(self, fileobj: Any, *, retry: Any = None) -> None:
        """No-op upload in replay mode.

        Args:
            fileobj: Ignored.
            retry: Ignored.
        """

    def download_as_bytes(self, *, retry: Any = None) -> bytes:
        """Return the recorded response bytes.

        Args:
            retry: Ignored.

        Returns:
            The bytes captured in the fixture GET response.
        """
        return self._content

    def delete(self, *args: Any, retry: Any = None, **kwargs: Any) -> None:
        """No-op delete in replay mode.

        Args:
            *args: Ignored.
            retry: Ignored.
            **kwargs: Ignored.
        """

    def reload(self) -> None:
        """No-op reload in replay mode."""

    def generate_signed_url(self, *, version: str, expiration: Any, method: str) -> str:
        """Return a synthetic signed URL satisfying wire-shape invariants.

        Args:
            version: Signing version (e.g. ``"v4"``).
            expiration: Timedelta or seconds.
            method: ``"GET"`` or ``"PUT"``.

        Returns:
            A synthetic HTTPS signed URL string.
        """
        return (
            f"https://storage.googleapis.com/{self.name}"
            f"?X-Goog-Signature=<REDACTED>&method={method}"
        )


class _FixtureReplayGCSBucket:
    """A fixture-backed bucket exposing the surface GCSArtifactStore calls.

    Args:
        name: Bucket name.
        blobs: Pre-populated mapping of blob-name → :class:`_FixtureReplayGCSBlob`.
    """

    def __init__(self, name: str, blobs: dict[str, _FixtureReplayGCSBlob]) -> None:
        self.name = name
        self._blobs = blobs

    def blob(self, key: str) -> _FixtureReplayGCSBlob:
        """Return the fixture-backed blob for *key*.

        Falls back to an empty blob if *key* is not in the fixture.

        Args:
            key: Blob name.

        Returns:
            :class:`_FixtureReplayGCSBlob` instance.
        """
        if key in self._blobs:
            return self._blobs[key]
        return _FixtureReplayGCSBlob(key, None, b"")

    def list_blobs(
        self, *, prefix: str = "", retry: Any = None
    ) -> list[_FixtureReplayGCSBlob]:
        """Return blobs matching *prefix*.

        Args:
            prefix: Key prefix filter.
            retry: Ignored.

        Returns:
            List of :class:`_FixtureReplayGCSBlob` instances.
        """
        return [b for name, b in sorted(self._blobs.items()) if name.startswith(prefix)]


class FixtureReplayGCSClient:
    """GCS client surface backed by pre-captured HTTP-level fixture JSON.

    The GCS recorder captures raw HTTPS round-trips.  This client parses
    those entries to reconstruct a blob-level surface for offline tests:

    - ``POST`` resumable-upload initiation → ignored (no useful response body).
    - ``PUT``  resumable-upload completion → JSON body = blob metadata.
    - ``GET``  download                   → raw bytes body = blob content.
    - ``DELETE``                          → ignored.

    The blob name and bucket are extracted from the metadata JSON (``name``
    and ``bucket`` fields present in GCS upload-complete responses).

    Args:
        fixture_path: Path to a previously captured ``.json`` fixture file.
    """

    def __init__(self, fixture_path: Path) -> None:
        raw = json.loads(Path(fixture_path).read_text())
        self._meta: dict[str, Any] = raw.get("_meta", {})
        entries: list[dict[str, Any]] = raw["entries"]

        # Build blob map: bucket_name → {blob_name → blob}
        self._buckets: dict[str, dict[str, _FixtureReplayGCSBlob]] = {}
        self._parse_entries(entries)

    def _parse_entries(self, entries: list[dict[str, Any]]) -> None:
        """Parse HTTP entries and populate ``_buckets``.

        Two-pass algorithm so that GET (download) entries recorded AFTER PUT
        (upload) entries in the same fixture are still applied to the correct
        blob.  A single forward pass would construct the blob with empty content
        because the download cache is not yet populated when the PUT is processed.

        - Pass 1: walk all entries and populate ``download_cache`` from GET
          responses.
        - Pass 2: walk all entries again and construct blobs from PUT responses,
          now looking up the pre-populated cache.

        Args:
            entries: Raw list of fixture entry dicts.
        """
        import urllib.parse

        # Pass 1: populate download_cache from all GET responses.
        download_cache: dict[str, bytes] = {}
        for entry in entries:
            method = entry.get("method", "")
            status = entry.get("status", 0)
            if method == "GET" and status == 200:
                body_bytes = base64.b64decode(entry.get("body_b64", ""))
                url = entry.get("url", "")
                raw_blob_name = _extract_gcs_blob_name(url)
                if raw_blob_name:
                    download_cache[urllib.parse.unquote(raw_blob_name)] = body_bytes

        # Pass 2: construct blobs from PUT responses, using the cache.
        for entry in entries:
            method = entry.get("method", "")
            status = entry.get("status", 0)
            if method == "PUT" and status == 200:
                body_bytes = base64.b64decode(entry.get("body_b64", ""))
                if not body_bytes:
                    continue
                # Resumable upload completion — body is blob metadata JSON.
                try:
                    meta = json.loads(body_bytes)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                blob_name: str = meta.get("name", "")
                bucket_name: str = meta.get("bucket", "")
                if not blob_name or not bucket_name:
                    continue
                content = download_cache.get(blob_name, b"")
                blob = _FixtureReplayGCSBlob(blob_name, meta, content)
                self._buckets.setdefault(bucket_name, {})[blob_name] = blob

    def bucket(self, name: str) -> _FixtureReplayGCSBucket:
        """Return the fixture-backed bucket for *name*.

        Args:
            name: Bucket name.

        Returns:
            :class:`_FixtureReplayGCSBucket` with blobs parsed from fixture.
        """
        return _FixtureReplayGCSBucket(name, self._buckets.get(name, {}))


def _extract_gcs_blob_name(url: str) -> str:
    """Extract the blob name from a GCS download URL.

    Expected pattern:
    ``https://storage.googleapis.com/download/storage/v1/b/<bucket>/o/<blob>?...``

    Args:
        url: Full GCS HTTPS URL string.

    Returns:
        The blob name segment (URL-encoded), or empty string if not matched.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    path = parsed.path  # e.g. /download/storage/v1/b/bucket/o/blob%2Fname
    # Split on /o/
    parts = path.split("/o/", 1)
    if len(parts) == 2:
        return parts[1]
    return ""
