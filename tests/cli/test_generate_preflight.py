"""_cmd_generate preflight + --skip-preflight flag tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kinoforge.cli._main import main

_VALID_CFG = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "local:fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: "fake:latest"
  mode: pod
  lifecycle:
    budget: 1.0
"""


def test_generate_has_skip_preflight_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["generate", "--help"])
    out = capsys.readouterr().out
    assert "--skip-preflight" in out


def test_generate_skip_preflight_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--skip-preflight emits the warning then short-circuits before
    orchestrator dispatch. We don't actually generate — monkeypatch
    `kinoforge.cli._commands.generate` to a stub so the test exercises
    only the preflight branch.
    """
    from unittest.mock import MagicMock

    def _gen_stub(cfg: object, request: object, **kw: object) -> tuple[object, object]:
        artifact = MagicMock()
        artifact.uri = "test://x"
        return (artifact, None)

    monkeypatch.setattr("kinoforge.cli._commands.generate", _gen_stub)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_VALID_CFG)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with caplog.at_level(logging.WARNING, logger="kinoforge.cli._commands"):
        main(
            [
                "--state-dir",
                str(state_dir),
                "generate",
                "--config",
                str(cfg_path),
                "--prompt",
                "x",
                "--mode",
                "t2v",
                "--no-output-dir",
                "--skip-preflight",
            ]
        )
    assert any("preflight skipped" in r.getMessage() for r in caplog.records)
