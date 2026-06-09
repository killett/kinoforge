"""E2E: vault loaded → no log record contains the prompt body.

Drives the real CLI with a vault registered, captures every log record
across the ``kinoforge`` logger hierarchy, and asserts the prompt body
canary never appears verbatim. The ``RedactingLogFilter`` installed by
``cli/_main.py`` carries this guarantee on both the root and the
``kinoforge`` logger.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml


def _minimal_fake_config_yaml(state_root: Path, output_dir: Path) -> str:
    return yaml.safe_dump(
        {
            "engine": {"kind": "fake", "precision": "fp16"},
            "models": [
                {
                    "ref": "https://example.com/fake-base.safetensors",
                    "kind": "base",
                    "target": "checkpoints",
                }
            ],
            "compute": {
                "provider": "local",
                "image": "kinoforge/local:latest",
            },
            "store": {"kind": "local", "root": str(state_root)},
            "output": {"kind": "local", "dir": str(output_dir), "enabled": True},
        }
    )


def test_no_log_record_contains_prompt_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Vault-registered prompt body never appears in any log record.

    Would-fail-bug: a missing ``RedactingLogFilter`` install on the root
    logger would let third-party libraries (urllib3, runpod-sdk) leak
    interpolated prompt strings via their own loggers; a missing filter
    on ``kinoforge`` would leak from kinoforge's own modules.
    """
    vault_path = tmp_path / "vault.yaml"
    canary = "MAGIC-CANARY-PROMPT-DO-NOT-LOG"
    vault_path.write_text(yaml.safe_dump({"positive_prompt": canary}))
    vault_path.chmod(0o600)

    state_dir = tmp_path / "state"
    store_root = tmp_path / "store"
    output_dir = tmp_path / "output"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_minimal_fake_config_yaml(store_root, output_dir))

    from kinoforge.cli import main

    with caplog.at_level(logging.DEBUG):
        rc = main(
            [
                "--state-dir",
                str(state_dir),
                "--vault",
                str(vault_path),
                "generate",
                "-c",
                str(config_path),
                "--prompt",
                "ignored",
                "--mode",
                "t2v",
                "--run-id",
                "test-run-canary",
            ]
        )
        assert rc == 0
        # The CLI run above installs the RedactingLogFilter; emit a log
        # record from a kinoforge-namespace logger that interpolates the
        # canary. If the filter was installed, the record passes through
        # it and the canary is substituted before caplog captures it.
        # If the install was skipped or removed, the canary survives
        # verbatim and the assertion below fires.
        logging.getLogger("kinoforge.test_canary").info(
            "prompt body for diagnostics: %s", canary
        )

    for record in caplog.records:
        msg = record.getMessage()
        assert canary not in msg, f"prompt canary leaked in log: {msg!r}"
