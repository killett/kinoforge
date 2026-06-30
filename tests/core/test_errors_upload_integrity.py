"""UploadIntegrityError surface contract."""

from kinoforge.core.errors import KinoforgeError, UploadIntegrityError


def test_upload_integrity_error_is_kinoforge_error() -> None:
    """UploadIntegrityError should be catchable as KinoforgeError."""
    exc = UploadIntegrityError(
        local_sha256="a" * 64,
        server_sha256="b" * 64,
        bytes_sent=1024,
    )
    assert isinstance(exc, KinoforgeError)
    assert exc.local_sha256 == "a" * 64
    assert exc.server_sha256 == "b" * 64
    assert exc.bytes_sent == 1024


def test_upload_integrity_error_str_mentions_both_hashes() -> None:
    """str() must include both hashes so operators can grep logs."""
    exc = UploadIntegrityError(
        local_sha256="abc" + "0" * 61,
        server_sha256="def" + "0" * 61,
        bytes_sent=42,
    )
    msg = str(exc)
    assert "abc" in msg
    assert "def" in msg
    assert "42" in msg
