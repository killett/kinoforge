"""kinoforge logs CLI — fetch /tmp/bootstrap.log via port-8001 sidecar."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _ns(**kwargs: object) -> argparse.Namespace:
    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def _ctx(tmp_path: Path) -> SessionContext:
    from kinoforge.cli.context import SessionContext

    return SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)


from kinoforge.cli.context import SessionContext  # noqa: E402 — after helper decl


def _fake_response(body: bytes, status: int = 200) -> MagicMock:
    """Return a MagicMock mimicking the urlopen context-manager response."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body)
    resp.status = status
    return resp


def test_cmd_logs_default_hits_bootstrap_log_on_port_8001(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug caught: URL shape drifts (wrong port, wrong file path) → operator
    fetches 404 from a live pod they know is running. The port-8001 sidecar
    and /bootstrap.log path are contracted by wan_t2v_server's bootstrap
    script — the CLI must match.
    """
    from kinoforge.cli import _commands

    fake_body = b"bootstrap-log-content-line-1\nline-2\n"
    with patch(
        "kinoforge.cli._commands.urllib.request.urlopen",
        return_value=_fake_response(fake_body),
    ) as urlopen:
        rc = _commands._cmd_logs(
            _ns(id="podabc123", file="bootstrap.log", out=None),
            _ctx(tmp_path),
        )
    assert rc == 0
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://podabc123-8001.proxy.runpod.net/bootstrap.log"
    out = capsys.readouterr().out
    assert out == fake_body.decode()


def test_cmd_logs_out_writes_bytes_to_file(tmp_path: Path) -> None:
    """Bug caught: --out silently drops bytes to stdout while pretending to
    write the file (missing dest.write_bytes) → next-session forensics has
    no artifact to inspect and operator has to re-fire the smoke.
    """
    from kinoforge.cli import _commands

    fake_body = b"file-write-body\n"
    dest = tmp_path / "captured.log"
    with patch(
        "kinoforge.cli._commands.urllib.request.urlopen",
        return_value=_fake_response(fake_body),
    ):
        rc = _commands._cmd_logs(
            _ns(id="pod9x", file="bootstrap.log", out=str(dest)),
            _ctx(tmp_path),
        )
    assert rc == 0
    assert dest.read_bytes() == fake_body


def test_cmd_logs_http_error_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug caught: exception path returns 0 → CI green while log fetch failed
    silently; wrapper scripts that pipe the CLI to grep think the pod had no
    matching lines when actually the fetch never succeeded.
    """
    from urllib.error import HTTPError

    from kinoforge.cli import _commands

    err = HTTPError(
        "https://podz-8001.proxy.runpod.net/bootstrap.log",
        503,
        "Service Unavailable",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b""),
    )
    with patch(
        "kinoforge.cli._commands.urllib.request.urlopen",
        side_effect=err,
    ):
        rc = _commands._cmd_logs(
            _ns(id="podz", file="bootstrap.log", out=None),
            _ctx(tmp_path),
        )
    assert rc == 1
    err_text = capsys.readouterr().err
    assert "503" in err_text


def test_cmd_logs_custom_file_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug caught: --file gets ignored → operator can't fetch selfterm.log or
    any sidecar-served file besides bootstrap.log, defeating the purpose of
    the port-8001 http.server (which serves --directory /tmp).
    """
    from kinoforge.cli import _commands

    with patch(
        "kinoforge.cli._commands.urllib.request.urlopen",
        return_value=_fake_response(b"selfterm-content"),
    ) as urlopen:
        rc = _commands._cmd_logs(
            _ns(id="podQ", file="selfterm.log", out=None),
            _ctx(tmp_path),
        )
    assert rc == 0
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://podQ-8001.proxy.runpod.net/selfterm.log"
