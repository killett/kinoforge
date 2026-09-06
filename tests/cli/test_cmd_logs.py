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


class _FakeLedger:
    """Minimal ledger stand-in — ``entries()`` is the only surface used."""

    def __init__(self, entries: list[dict[str, object]]) -> None:
        self._entries = entries

    def entries(self) -> list[dict[str, object]]:
        return self._entries


class _FakeCtx:
    """SessionContext stand-in exposing only ``ledger()``.

    ``raises`` models the cloud-store failure mode (expired credentials),
    where merely building the ledger throws.
    """

    def __init__(
        self,
        entries: list[dict[str, object]] | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._ledger = _FakeLedger(entries or [])
        self._raises = raises

    def ledger(self) -> _FakeLedger:
        if self._raises is not None:
            raise self._raises
        return self._ledger


def test_cmd_logs_refuses_when_ledger_says_modal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bug caught: the handler discards the ledger and builds a RunPod proxy
    hostname from any id, so on a Modal pod it fetches a host that never
    existed and reports `HTTP 404 Not Found` — which the operator reads as
    "this pod has no log" rather than "this command is RunPod-only", while a
    live pod is still billing (matrix cell T1-04).

    The refusal must name the provider, must not touch the network, and must
    exit non-zero with no traceback.
    """
    from kinoforge.cli import _commands

    ctx = _FakeCtx([{"id": "run-20260906-010255", "provider": "modal"}])
    with patch("kinoforge.cli._commands.urllib.request.urlopen") as urlopen:
        rc = _commands._cmd_logs(
            _ns(id="run-20260906-010255", file="bootstrap.log", out=None),
            ctx,  # type: ignore[arg-type]
        )
    assert rc == 2
    urlopen.assert_not_called()
    err = capsys.readouterr().err
    assert "modal" in err
    assert "run-20260906-010255" in err
    assert "proxy.runpod.net" not in err
    assert "Traceback" not in err


def test_cmd_logs_refusal_does_not_write_out_file(tmp_path: Path) -> None:
    """Bug caught: the provider guard lands after the write block, so a
    refused fetch still creates the ``--out`` file. A zero-byte
    ``server.log`` on disk reads to later forensics as "the pod produced no
    output" — the opposite of what happened.
    """
    from kinoforge.cli import _commands

    dest = tmp_path / "server.log"
    ctx = _FakeCtx([{"id": "run-x", "provider": "modal"}])
    with patch("kinoforge.cli._commands.urllib.request.urlopen") as urlopen:
        rc = _commands._cmd_logs(
            _ns(id="run-x", file="server.log", out=str(dest)),
            ctx,  # type: ignore[arg-type]
        )
    assert rc == 2
    urlopen.assert_not_called()
    assert not dest.exists()


def test_cmd_logs_runpod_ledger_entry_still_fetches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bug caught: an inverted or overbroad guard (wrong dict key, or
    refusing whenever the id resolves at all) would refuse on RunPod too,
    breaking the one provider whose port-8001 sidecar this command exists to
    read.
    """
    from kinoforge.cli import _commands

    ctx = _FakeCtx([{"id": "podrp", "provider": "runpod"}])
    with patch(
        "kinoforge.cli._commands.urllib.request.urlopen",
        return_value=_fake_response(b"rp-body"),
    ) as urlopen:
        rc = _commands._cmd_logs(
            _ns(id="podrp", file="bootstrap.log", out=None),
            ctx,  # type: ignore[arg-type]
        )
    assert rc == 0
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://podrp-8001.proxy.runpod.net/bootstrap.log"
    assert capsys.readouterr().out == "rp-body"


def test_cmd_logs_id_absent_from_ledger_falls_back_to_fetch() -> None:
    """Bug caught: a guard that refuses whenever the ledger lookup misses
    breaks the command's primary forensic use — fetching bootstrap.log from a
    pod that ``destroy`` or ``forget`` has already removed from the ledger,
    which is exactly when the operator most needs it.
    """
    from kinoforge.cli import _commands

    ctx = _FakeCtx([{"id": "someone-else", "provider": "modal"}])
    with patch(
        "kinoforge.cli._commands.urllib.request.urlopen",
        return_value=_fake_response(b"gone-pod-body"),
    ) as urlopen:
        rc = _commands._cmd_logs(
            _ns(id="podgone", file="bootstrap.log", out=None),
            ctx,  # type: ignore[arg-type]
        )
    assert rc == 0
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://podgone-8001.proxy.runpod.net/bootstrap.log"


def test_cmd_logs_ledger_unavailable_does_not_traceback() -> None:
    """Bug caught: consulting the ledger unguarded turns an unrelated store
    failure (expired cloud credentials) into a stack trace on a command that
    never needed the store — the fetch is still perfectly possible from the id
    alone, so an unreadable ledger must degrade to the RunPod path, not crash.
    """
    from kinoforge.cli import _commands

    ctx = _FakeCtx(raises=RuntimeError("ExpiredToken: credentials rejected"))
    with patch(
        "kinoforge.cli._commands.urllib.request.urlopen",
        return_value=_fake_response(b"degraded-body"),
    ) as urlopen:
        rc = _commands._cmd_logs(
            _ns(id="podnl", file="bootstrap.log", out=None),
            ctx,  # type: ignore[arg-type]
        )
    assert rc == 0
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://podnl-8001.proxy.runpod.net/bootstrap.log"
