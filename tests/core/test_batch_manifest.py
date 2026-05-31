"""Tests for kinoforge.core.batch — manifest schema + loader (Layer L Task 2)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.batch import (
    load_manifest,
)
from kinoforge.core.errors import ConfigError


def _write_yaml(path: Path, data: list[dict[str, object]]) -> Path:
    """Dump *data* to *path* as YAML; return *path* for chaining."""
    path.write_text(yaml.safe_dump(data))
    return path


def test_load_manifest_round_trip_three_entries(tmp_path: Path) -> None:
    """A 3-entry YAML list must produce a 3-entry BatchManifest.

    Bug catch: a parser that silently drops entries (e.g. a generator
    that exhausts early) ships an under-counted batch with no error.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [
            {"prompt": "a", "mode": "t2v", "run_id": "x"},
            {"prompt": "b", "mode": "t2v", "run_id": "y"},
            {"prompt": "c", "mode": "t2v", "run_id": "z"},
        ],
    )
    m = load_manifest(path)
    assert len(m.entries) == 3
    assert [e.run_id for e in m.entries] == ["x", "y", "z"]
    assert [e.prompt for e in m.entries] == ["a", "b", "c"]


def test_entry_with_both_prompt_and_prompt_file_raises(tmp_path: Path) -> None:
    """An entry with both prompt and prompt_file is ambiguous — must reject.

    Bug catch: ambiguity in the input shape silently picks one (whichever
    pydantic sees first) and discards the other — user thinks they wrote
    one prompt, the engine sees a different one.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt": "a", "prompt_file": "x.txt", "mode": "t2v"}],
    )
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "exactly one of `prompt` / `prompt_file`" in str(exc_info.value)


def test_entry_with_neither_prompt_nor_prompt_file_raises(tmp_path: Path) -> None:
    """An entry with neither source — must reject.

    Bug catch: empty entry silently produces an empty prompt that fails
    downstream in the engine with a confusing message.
    """
    path = _write_yaml(tmp_path / "m.yaml", [{"mode": "t2v"}])
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "exactly one of `prompt` / `prompt_file`" in str(exc_info.value)


def test_prompt_file_resolves_relative_to_manifest_dir(tmp_path: Path) -> None:
    """prompt_file paths are resolved against the manifest's parent dir.

    Bug catch: resolving against CWD breaks any invocation where the user
    runs `kinoforge batch` from a directory other than the one containing
    the manifest — the silent footgun is wide.
    """
    sub = tmp_path / "configs"
    sub.mkdir()
    prompt_path = sub / "forest.txt"
    prompt_path.write_text("forest at dawn")
    manifest_path = _write_yaml(
        sub / "m.yaml",
        [{"prompt_file": "forest.txt", "mode": "t2v", "run_id": "f"}],
    )
    m = load_manifest(manifest_path)
    assert m.entries[0].prompt == "forest at dawn"
    assert m.entries[0].prompt_file is None  # collapsed to inline


def test_missing_prompt_file_raises_config_error_with_path(tmp_path: Path) -> None:
    """A missing prompt_file must produce ConfigError naming the path.

    Bug catch: a bare FileNotFoundError leaves the user grepping for which
    entry was bad. We need both the resolved path and the entry's mode in
    the message.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt_file": "nope.txt", "mode": "t2v", "run_id": "f"}],
    )
    with pytest.raises(ConfigError) as exc_info:
        load_manifest(path)
    assert "nope.txt" in str(exc_info.value)
    assert "t2v" in str(exc_info.value)


def test_prompt_file_strips_trailing_whitespace(tmp_path: Path) -> None:
    """Trailing newlines on prompt_file content must be stripped.

    Bug catch: a literal trailing newline poisons engines that validate
    prompt length or hash the prompt for caching — silent retries.
    """
    (tmp_path / "p.txt").write_text("hello world\n\n")
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt_file": "p.txt", "mode": "t2v", "run_id": "p"}],
    )
    m = load_manifest(path)
    assert m.entries[0].prompt == "hello world"


def test_duplicate_explicit_run_ids_raise(tmp_path: Path) -> None:
    """Two entries with the same explicit run_id — must reject.

    Bug catch: silent overlap in the store namespace; second artifact
    overwrites the first.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [
            {"prompt": "a", "mode": "t2v", "run_id": "same"},
            {"prompt": "b", "mode": "t2v", "run_id": "same"},
        ],
    )
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "same" in str(exc_info.value)
    assert "duplicate run_id" in str(exc_info.value)


def test_unknown_per_entry_key_raises_via_extra_forbid(tmp_path: Path) -> None:
    """Per-entry `engine: foo` (unsupported override) — must reject.

    Bug catch: silently accepted per-entry engine override breaks the
    shared-deploy assumption — batch ships wrong-engine artifacts.
    """
    path = _write_yaml(
        tmp_path / "m.yaml",
        [{"prompt": "a", "mode": "t2v", "engine": "foo"}],
    )
    with pytest.raises(PydanticValidationError) as exc_info:
        load_manifest(path)
    assert "engine" in str(exc_info.value).lower()
