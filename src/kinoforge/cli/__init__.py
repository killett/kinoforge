"""Back-compat re-export surface for the kinoforge.cli package.

The CLI internals live in ``cli._main`` + ``cli._commands``. This shim
preserves every import path that tests and the entry point rely on::

    from kinoforge.cli import main           # used by __main__.py
    from kinoforge.cli import _build_store   # used by tests/test_cli.py
    from kinoforge.cli import _build_parser  # used by tests/test_cli.py
    from kinoforge.cli import _build_ledger_block  # used by tests/test_cli.py
"""

from kinoforge.cli._commands import (
    _build_ledger_block,
    _build_sink,
    _build_store,
    _cli_clock,
    _cmd_batch,
    _cmd_deploy,
    _cmd_destroy,
    _cmd_forget,
    _cmd_gc,
    _cmd_generate,
    _cmd_list,
    _cmd_provision,
    _cmd_reap,
    _cmd_status,
    _cmd_stop,
    _ledger,
    _ledger_field_or_cfg,
    _print_status_block,
)
from kinoforge.cli._main import _build_parser, _print_instance_overview, main
from kinoforge.core.orchestrator import generate

__all__ = [
    "_build_ledger_block",
    "_build_parser",
    "_build_sink",
    "_build_store",
    "_cli_clock",
    "_cmd_batch",
    "_cmd_deploy",
    "_cmd_destroy",
    "_cmd_forget",
    "_cmd_gc",
    "_cmd_generate",
    "_cmd_list",
    "_cmd_provision",
    "_cmd_reap",
    "_cmd_status",
    "_cmd_stop",
    "_ledger",
    "_ledger_field_or_cfg",
    "_print_instance_overview",
    "_print_status_block",
    "generate",
    "main",
]
