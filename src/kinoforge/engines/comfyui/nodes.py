"""Git-based custom-node installer for ComfyUI.

Clones each node repository and optionally installs its ``requirements.txt``
when present.  All I/O is channelled through injected callables so tests can
spy without real subprocess or filesystem access.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def clone_and_install(
    *,
    node_entries: list[dict[str, Any]],
    comfyui_root: str,
    run_cmd: Callable[[list[str], str | None], None],
    file_exists: Callable[[str], bool],
) -> None:
    """Clone each custom-node repo and install its ``requirements.txt``.

    For each entry in *node_entries*, the function:

    1. Derives a destination directory from the repository URL (the last
       path component, stripped of a ``.git`` suffix).
    2. Issues ``git clone <url> <dest_dir>`` via *run_cmd*.
    3. Checks whether ``<dest_dir>/requirements.txt`` exists using
       *file_exists*; if it does, runs ``pip install -r <path>`` via
       *run_cmd*.

    Args:
        node_entries: List of node config dicts; each must have a ``"git"``
            key whose value is the repository URL.
        comfyui_root: Absolute path to the ComfyUI installation directory.
            Custom nodes are cloned under ``<comfyui_root>/custom_nodes/``.
        run_cmd: Callable ``(argv, cwd) -> None``; invoked for both git and
            pip commands.
        file_exists: Callable ``(path) -> bool``; returns ``True`` when the
            given filesystem path exists.
    """
    custom_nodes_dir = os.path.join(comfyui_root, "custom_nodes")
    for entry in node_entries:
        url: str = entry["git"]
        # Derive a local directory name from the repository URL.
        repo_name = url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        dest = os.path.join(custom_nodes_dir, repo_name)
        run_cmd(["git", "clone", url, dest], None)
        req_path = os.path.join(dest, "requirements.txt")
        if file_exists(req_path):
            run_cmd(["pip", "install", "-r", req_path], dest)
