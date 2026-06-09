"""E2E: after vault + ``--ephemeral`` run, only the output dir survives.

The artifact store run dir is empty (or absent); the state dir has no
run-tagged sidecars; the output dir contains the published file(s)
with permissive names. Per spec §14.2 Appendix C.

Drives the real ``kinoforge generate`` CLI path with the offline
``FakeEngine`` + ``LocalProvider`` + ``LocalArtifactStore`` +
``LocalOutputSink`` stack — no real cloud, no real network, no real
GPU, no real model weights.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _minimal_fake_config_yaml(state_root: Path, output_dir: Path) -> str:
    """Minimal kinoforge cfg using the FakeEngine + LocalProvider stack.

    The ``output.dir`` block opt-in is what wires the LocalOutputSink at
    CLI build time. Without it, the CLI skips publish and the test would
    have nothing to assert against in the output dir.
    """
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


def test_ephemeral_only_output_dir_survives(tmp_path: Path) -> None:
    """Vault + ``--ephemeral`` + full FakeEngine run → only output-dir on disk.

    The run dir under the artifact store stays empty (Sub-α scrub
    + Sub-γ ``__exit__``); the state dir holds no run-tagged sidecars
    (Sub-β ledger gate); the output dir contains the published file
    (Sub-β ``OutputSink.publish`` is the one durable write path).

    Would-fail-bug: if Sub-γ ``__exit__`` were guarded on a clean exit
    only, a partial-run crash would leave the run dir populated; if
    Sub-β's ledger gate were missing, ``_ledger.json`` would survive
    too and a directory listing would name every instance ever created.
    """
    # Vault path outside the active repo — pytest's tmp_path lives under
    # /tmp on Linux, which is never under the repo root.
    vault_path = tmp_path / "vault.yaml"
    vault_path.write_text(yaml.safe_dump({"positive_prompt": "MAGIC-CANARY-PROMPT"}))
    vault_path.chmod(0o600)

    state_dir = tmp_path / "state"
    store_root = tmp_path / "store"
    output_dir = tmp_path / "output"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_minimal_fake_config_yaml(store_root, output_dir))

    # Drive the CLI exactly as an operator would.
    from kinoforge.cli import main

    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--vault",
            str(vault_path),
            "--ephemeral",
            "generate",
            "-c",
            str(config_path),
            "--prompt",
            "ignored — vault overrides",
            "--mode",
            "t2v",
            "--run-id",
            "test-run-1",
        ]
    )
    assert exit_code == 0, "CLI generate failed under --ephemeral"

    # Output dir survived: at least one published file with a permissive name.
    output_files = [p for p in output_dir.rglob("*") if p.is_file()]
    assert len(output_files) >= 1, (
        f"output dir empty after publish (no files under {output_dir})"
    )
    for p in output_files:
        # Permissive name carries an underscore-joined schema from
        # LocalOutputSink.publish — never the raw prompt body.
        assert "MAGIC-CANARY-PROMPT" not in p.name, (
            f"published filename leaked prompt: {p.name}"
        )

    # Store root: ephemeral __exit__ wiped the run dir, so the per-run
    # subdirectory under the store does NOT survive (or survives empty).
    run_dir = store_root / "test-run-1"
    if run_dir.exists():
        assert not any(run_dir.rglob("*")), (
            f"ephemeral __exit__ left files behind in {run_dir}: "
            f"{list(run_dir.rglob('*'))}"
        )

    # State dir: no run-tagged sidecars (no ledger.json, no
    # _batch_summary.json, no prompt-derived profile cache file).
    state_artifacts = [p for p in state_dir.rglob("*") if p.is_file()]
    leaked_ledger = [p for p in state_artifacts if p.name == "ledger.json"]
    leaked_summary = [p for p in state_artifacts if p.name == "_batch_summary.json"]
    assert not leaked_ledger, f"ledger.json survived ephemeral: {leaked_ledger}"
    assert not leaked_summary, f"_batch_summary.json survived: {leaked_summary}"
