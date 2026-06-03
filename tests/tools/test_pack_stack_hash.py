"""Lockdown tests for tools._pack_stack.pack_stack_hash."""

from __future__ import annotations

from tools._pack_stack import pack_stack_hash


def test_empty_pack_stack_has_stable_hash() -> None:
    """Empty comfyui_cfg always hashes to the same pinned value.

    A drift here means the canonical-form algorithm changed (or the
    hash length / case convention shifted). The pinned value is the
    SHA256 prefix of the literal string "comfyui@" (no trailing
    newline, since the parts list only has one element when
    custom_nodes is empty).
    """
    h = pack_stack_hash({"version": "", "custom_nodes": []})
    # Recomputable from spec §5.1: sha256("comfyui@")[:12]
    assert h == "2965808c10e9", f"empty pack-stack hash drifted: {h!r}"


def test_pack_stack_hash_is_order_insensitive() -> None:
    """Two equivalent stacks in different YAML orders produce the same hash."""
    cfg_a = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/A", "ref": "abc"},
            {"git": "https://github.com/kijai/B", "ref": "def"},
        ],
    }
    cfg_b = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/B", "ref": "def"},
            {"git": "https://github.com/kijai/A", "ref": "abc"},
        ],
    }
    assert pack_stack_hash(cfg_a) == pack_stack_hash(cfg_b)


def test_pack_stack_hash_changes_on_ref_bump() -> None:
    """Bumping any pinned ref produces a different hash."""
    base = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/A", "ref": "abc"},
        ],
    }
    bumped = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/A", "ref": "abd"},  # one char
        ],
    }
    assert pack_stack_hash(base) != pack_stack_hash(bumped)
