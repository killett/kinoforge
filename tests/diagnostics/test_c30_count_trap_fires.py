"""Unit tests for ``c30_probe.count_trap_fires``."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from kinoforge.diagnostics.c30_probe import count_trap_fires


class _StubS3:
    """Minimal stub for the boto3 S3 client surface ``count_trap_fires`` uses."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        idx = len(self.calls) - 1
        if idx >= len(self._pages):
            return {}
        return self._pages[idx]


def test_returns_count_of_diag_files() -> None:
    s3 = _StubS3(
        [
            {
                "Contents": [
                    {"Key": "p/diag-20260614T000000Z.txt"},
                    {"Key": "p/diag-20260614T000030Z.txt"},
                ],
            }
        ]
    )
    assert count_trap_fires(s3, "bkt", "p/") == 2


def test_ignores_non_diag_keys() -> None:
    s3 = _StubS3(
        [
            {
                "Contents": [
                    {"Key": "p/diag-20260614T000000Z.txt"},
                    {"Key": "p/notes.txt"},
                    {"Key": "p/diag-20260614T000030Z.txt.bak"},
                ],
            }
        ]
    )
    assert count_trap_fires(s3, "bkt", "p/") == 1


def test_paginates_correctly() -> None:
    s3 = _StubS3(
        [
            {
                "Contents": [
                    {"Key": "p/diag-20260614T000000Z.txt"},
                    {"Key": "p/diag-20260614T000030Z.txt"},
                ],
                "IsTruncated": True,
                "NextContinuationToken": "tok",
            },
            {"Contents": [{"Key": "p/diag-20260614T000060Z.txt"}]},
        ]
    )
    assert count_trap_fires(s3, "bkt", "p/") == 3
    assert s3.calls[1]["ContinuationToken"] == "tok"


def test_empty_prefix_returns_zero() -> None:
    s3 = _StubS3([{}])
    assert count_trap_fires(s3, "bkt", "p/") == 0


def test_no_such_key_returns_zero() -> None:
    class _NoSuchKey:
        def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "absent"}},
                "ListObjectsV2",
            )

    assert count_trap_fires(_NoSuchKey(), "bkt", "p/") == 0
