"""Tests for kinoforge.core.dotenv_loader.

All tests are offline. They use ``tmp_path`` for the .env file and
``monkeypatch`` for ``os.environ`` mutations so the host environment is
never touched.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kinoforge.core.dotenv_loader import load_env_file


def _write_env(path: Path, content: str) -> None:
    """Write *content* to *path* with a trailing newline."""
    path.write_text(content + "\n", encoding="utf-8")


def test_absent_default_path_is_silent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No .env at default path → no-op, no log, no exception."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAL_KEY", raising=False)
    caplog.set_level(logging.INFO, logger="kinoforge.core.dotenv_loader")

    load_env_file()  # default path = cwd/.env, which does not exist

    assert os.environ.get("FAL_KEY") is None
    assert caplog.records == []


def test_loads_keys_into_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env containing FAL_KEY=abc populates os.environ['FAL_KEY']."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=abc")

    load_env_file(env_file)

    assert os.environ.get("FAL_KEY") == "abc"


def test_shell_value_wins_over_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell-set value persists; .env value is ignored (override=False)."""
    monkeypatch.setenv("FAL_KEY", "shell")
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=file")

    load_env_file(env_file)

    assert os.environ.get("FAL_KEY") == "shell"


def test_env_file_fills_unset_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys absent from the shell are filled from the .env file."""
    monkeypatch.setenv("FAL_KEY", "shell")
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=file\nCIVITAI_TOKEN=fromfile")

    load_env_file(env_file)

    assert os.environ.get("FAL_KEY") == "shell"
    assert os.environ.get("CIVITAI_TOKEN") == "fromfile"


def test_explicit_path_missing_raises_FileNotFoundError(tmp_path: Path) -> None:
    """An explicitly passed missing path raises FileNotFoundError."""
    missing = tmp_path / "nope.env"

    with pytest.raises(FileNotFoundError, match=str(missing)):
        load_env_file(missing)


def test_malformed_env_propagates_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env file with content python-dotenv rejects raises (not swallowed).

    python-dotenv is fairly permissive; the most reliably-failing case across
    versions is a file containing a non-UTF-8 byte sequence which fails
    decoding during read. The implementation MUST surface this rather than
    silently treating it as empty.
    """
    monkeypatch.delenv("FAL_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xff\xfeFAL_KEY=abc\n")  # invalid UTF-8 leading bytes

    with pytest.raises(UnicodeDecodeError):
        load_env_file(env_file)


def test_info_log_shows_count_and_path_not_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """INFO log mentions path + count, never the secret values."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=secret_value_abc\nCIVITAI_TOKEN=tok_xyz")
    caplog.set_level(logging.INFO, logger="kinoforge.core.dotenv_loader")

    load_env_file(env_file)

    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert str(env_file) in messages
    assert "2" in messages  # key count
    assert "secret_value_abc" not in messages
    assert "tok_xyz" not in messages


def test_two_calls_idempotent_under_override_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call leaves already-set values unchanged."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=first")

    load_env_file(env_file)
    assert os.environ.get("FAL_KEY") == "first"

    # Rewrite the file with a different value; without override=True the
    # already-set value MUST persist.
    _write_env(env_file, "FAL_KEY=second")
    load_env_file(env_file)
    assert os.environ.get("FAL_KEY") == "first"
