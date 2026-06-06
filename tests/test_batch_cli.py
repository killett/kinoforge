"""End-to-end CLI tests for ``kinoforge batch`` (Layer L Task 4).

These tests drive ``kinoforge.cli.main`` directly (no subprocess) and use
``capsys`` for stdout/stderr capture, matching the pattern in
``tests/test_cli.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kinoforge.cli import main


def _write_local_fake_cfg(tmp_path: Path) -> Path:
    """Write a minimal local + FakeEngine config and return its path.

    The ``output:`` block is pinned to ``tmp_path / "output"`` so that
    sink publishes from FakeEngine-driven batch runs don't leak 24-byte
    placeholder MP4s into the real repo's ``output/`` (default behaviour
    of ``OutputConfig`` is to write relative to cwd, which is the repo
    root under pytest).
    """
    cfg = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {
                "ref": "https://example.com/fake.safetensors",
                "kind": "base",
                "target": "checkpoints",
            }
        ],
        "compute": {"provider": "local", "image": ""},
        "output": {
            "kind": "local",
            "dir": str(tmp_path / "output"),
            "enabled": True,
        },
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_kinoforge_batch_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """3-entry batch on local-fake cfg -> exit 0, summary printed.

    Bug catch: CLI wiring drops the new subcommand from the dispatcher, so
    every invocation prints help and returns 0 without running anything.
    A summary table verifies both the dispatch and the per-entry render.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest = [
        {"prompt": "a", "mode": "t2v", "run_id": "x"},
        {"prompt": "b", "mode": "t2v", "run_id": "y"},
        {"prompt": "c", "mode": "t2v", "run_id": "z"},
    ]
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))

    state_dir = tmp_path / "state"
    rc = main(
        [
            "--state-dir",
            str(state_dir),
            "batch",
            "-c",
            str(cfg_path),
            "--manifest",
            str(manifest_path),
            "--batch-id",
            "b",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "summary" in out.lower()
    for rid in ("x", "y", "z"):
        assert rid in out
    summary = json.loads((state_dir / "b" / "_batch_summary.json").read_text())
    assert summary["batch_id"] == "b"
    assert len(summary["entries"]) == 3


def test_missing_manifest_arg_exits_with_argparse_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse must demand --manifest.

    Bug catch: a default of '' silently accepts no manifest and dispatches
    against an empty list, producing a phantom success.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["batch", "-c", str(cfg_path)])
    assert exc_info.value.code == 2  # argparse standard exit code
    err = capsys.readouterr().err
    assert "--manifest" in err


def test_batch_id_collision_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing batch_id namespace must short-circuit with exit 1.

    Bug catch: silent overwrite of a prior batch's namespace destroys the
    user's earlier artifacts; the collision check must happen BEFORE any
    compute setup runs so no provisioner is touched.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        yaml.safe_dump([{"prompt": "a", "mode": "t2v", "run_id": "x"}])
    )
    state_dir = tmp_path / "state"
    # Pre-create a colliding namespace with at least one file.
    (state_dir / "existing").mkdir(parents=True)
    (state_dir / "existing" / "leftover.bin").write_bytes(b"hi")

    rc = main(
        [
            "--state-dir",
            str(state_dir),
            "batch",
            "-c",
            str(cfg_path),
            "--manifest",
            str(manifest_path),
            "--batch-id",
            "existing",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "batch_id collision" in err
    assert "existing" in err
    # No summary was written -- collision check is pre-compute.
    assert not (state_dir / "existing" / "_batch_summary.json").exists()


def test_zero_concurrent_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--concurrent 0 must be rejected before any work starts.

    Bug catch: 0 passed to ThreadPoolExecutor(max_workers=0) raises a
    confusing ValueError mid-batch; we want the CLI to fail fast with a
    clear message naming the offending flag.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        yaml.safe_dump([{"prompt": "a", "mode": "t2v", "run_id": "x"}])
    )
    state_dir = tmp_path / "state"
    rc = main(
        [
            "--state-dir",
            str(state_dir),
            "batch",
            "-c",
            str(cfg_path),
            "--manifest",
            str(manifest_path),
            "--concurrent",
            "0",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "--concurrent" in err
    assert "positive" in err


def test_unknown_engine_kind_exits_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cfg pointing at an unregistered engine must exit 1 with a clean line.

    Bug catch: setup-fatal exceptions from deploy_session.__enter__
    that aren't in the batch-fatal trio (BudgetExceeded, CapabilityMismatch,
    TeardownError) would otherwise escape as raw tracebacks, breaking
    the "every CLI failure path produces a clean stderr line + non-zero
    exit" contract that the rest of kinoforge CLI honours.
    """
    cfg_path = tmp_path / "bad-cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "engine": {"kind": "nonexistent_engine", "precision": "fp16"},
                "models": [
                    {
                        "ref": "https://example.com/x.safetensors",
                        "kind": "base",
                        "target": "checkpoints",
                    }
                ],
                "compute": {"provider": "local", "image": ""},
            }
        )
    )
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        yaml.safe_dump([{"prompt": "a", "mode": "t2v", "run_id": "x"}])
    )
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "batch",
            "-c",
            str(cfg_path),
            "--manifest",
            str(manifest_path),
            "--batch-id",
            "b",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err.lower()
    assert "nonexistent_engine" in err or "UnknownAdapter" in err


def test_one_bad_entry_continues_others(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bogus mode on one entry must not abort the batch.

    Bug catch: any per-entry exception aborting the whole batch defeats
    the continue-on-error contract at the CLI surface; an overnight run
    would die on the first bad prompt instead of completing the good ones.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            [
                {"prompt": "a", "mode": "t2v", "run_id": "x"},
                {"prompt": "b", "mode": "nope", "run_id": "y"},
                {"prompt": "c", "mode": "t2v", "run_id": "z"},
            ]
        )
    )
    state_dir = tmp_path / "state"
    rc = main(
        [
            "--state-dir",
            str(state_dir),
            "batch",
            "-c",
            str(cfg_path),
            "--manifest",
            str(manifest_path),
            "--batch-id",
            "b",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "x" in out and "z" in out
    assert "y" in out
    summary = json.loads((state_dir / "b" / "_batch_summary.json").read_text())
    statuses = {e["run_id"]: e["status"] for e in summary["entries"]}
    assert statuses == {"x": "ok", "y": "fail", "z": "ok"}


def _run_batch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str] | None = None,
) -> tuple[int, str, str]:
    """Run `kinoforge batch` against a 3-entry local-fake manifest.

    Returns (exit_code, stdout, stderr) so callers can assert on each.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest = [
        {"prompt": "a", "mode": "t2v", "run_id": "x"},
        {"prompt": "b", "mode": "t2v", "run_id": "y"},
        {"prompt": "c", "mode": "t2v", "run_id": "z"},
    ]
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    state_dir = tmp_path / "state"

    args = [
        "--state-dir",
        str(state_dir),
        "batch",
        "-c",
        str(cfg_path),
        "--manifest",
        str(manifest_path),
        "--batch-id",
        "b",
    ]
    if extra_args:
        args.extend(extra_args)

    rc = main(args)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_stream_format_human_default_emits_per_entry_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC7. Default --stream-format=human emits one START + one terminal
    line per entry, plus the summary table.

    Bug: a wiring regression that drops on_event would silently revert
    Layer L-T4 to the pre-streaming behaviour (header + summary only).
    """
    rc, out, _err = _run_batch(tmp_path, capsys, extra_args=[])
    assert rc == 0
    assert out.count(" START ") == 3
    # "] OK " discriminates streaming-event OK lines from summary-table OK lines
    # (streaming: "[b] [1/x] OK 0.0s ..."  vs  summary: "  x  OK    ...").
    assert out.count("] OK ") == 3
    assert "summary:" in out
    assert "batch-id: b" in out


def test_stream_format_jsonl_pure_stdout_and_summary_terminator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC8. --stream-format=jsonl: every stdout line is a JSON object;
    final line carries kind='batch_summary'; manifest-loaded header is
    on stderr; no human summary table on stdout.

    Bug: a leaked `print(header)` on stdout in jsonl mode would corrupt
    downstream pipes like `kinoforge batch ... | jq .` with a non-JSON line.
    """
    rc, out, err = _run_batch(tmp_path, capsys, extra_args=["--stream-format=jsonl"])
    assert rc == 0

    # AC8 contract: stdout is PURE JSONL — every non-empty line must parse.
    # No filtering: a single non-JSON line on stdout breaks `| jq .` in
    # production, so the test must catch any leak.  The instance-overview
    # header and the manifest-loaded header are both routed to stderr in
    # jsonl mode (gated in cli/_main.py and _cmd_batch respectively).
    lines = [line for line in out.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]  # raises on any non-JSON line

    # Terminal object is the batch_summary marker.
    assert parsed[-1]["kind"] == "batch_summary"
    assert parsed[-1]["batch_id"] == "b"

    # No human summary table on stdout.
    assert "summary:" not in out
    # Both headers (manifest loaded + instance overview) on stderr.
    assert "manifest loaded" in err
    assert "[instance overview]" in err
    # Verify start + finish events for all 3 entries appear in the JSONL.
    starts = [p for p in parsed if p.get("kind") == "entry_start"]
    finishes = [p for p in parsed if p.get("kind") == "entry_finish"]
    assert len(starts) == 3
    assert len(finishes) == 3


def test_stream_format_none_preserves_pre_layer_behaviour(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC9. --stream-format=none: only the manifest-loaded header and the
    summary block reach stdout; no mid-run START lines.

    Bug: a wiring regression that routes NoOpFormatter.emit to
    HumanFormatter.emit would silently re-stream the per-entry lines
    despite the opt-out.
    """
    rc, out, _err = _run_batch(tmp_path, capsys, extra_args=["--stream-format=none"])
    assert rc == 0
    assert " START " not in out
    assert "manifest loaded" in out
    assert "summary:" in out
    assert "batch-id: b" in out


def test_stream_format_invalid_choice_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC10. argparse rejects --stream-format=xyz with exit code 2.

    Bug: a misspelled flag that silently fell back to the default
    instead of failing would mask configuration errors in CI.
    """
    cfg_path = _write_local_fake_cfg(tmp_path)
    manifest_path = tmp_path / "m.yaml"
    manifest_path.write_text(yaml.safe_dump([{"prompt": "x", "mode": "t2v"}]))

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--state-dir",
                str(tmp_path / "state"),
                "batch",
                "-c",
                str(cfg_path),
                "--manifest",
                str(manifest_path),
                "--stream-format=xyz",
            ]
        )
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "stream-format" in err.lower() or "invalid choice" in err.lower()
