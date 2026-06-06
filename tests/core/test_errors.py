"""Tests for kinoforge.core.errors — Layer T sidecar errors (Phase 34 T2)."""

from kinoforge.core.errors import (
    KinoforgeError,
    SidecarMigrationBlocked,
    SidecarMismatch,
)


def test_sidecar_mismatch_subclasses_kinoforge_error():
    """except KinoforgeError must catch SidecarMismatch — wiring contract.

    Bug-catch: a future edit that re-parents SidecarMismatch to bare
    Exception would silently break _cmd_batch's Setup-fatal catch arm
    in cli/__init__.py:527, letting the error escape as a raw traceback.
    """
    assert issubclass(SidecarMismatch, KinoforgeError)


def test_sidecar_migration_blocked_subclasses_kinoforge_error():
    """except KinoforgeError must catch SidecarMigrationBlocked — wiring contract.

    Bug-catch: a future edit that re-parents this error to bare Exception
    breaks _cmd_batch's Setup-fatal arm in cli/__init__.py:527 — the error
    would escape as a raw traceback instead of clean exit 1.
    """
    assert issubclass(SidecarMigrationBlocked, KinoforgeError)


def test_sidecar_mismatch_carries_message():
    """Exception message round-trips verbatim so the CLI prints it as-is.

    Bug-catch: a future SidecarMismatch override of __init__ that
    swallows the positional message (e.g. via super().__init__()) would
    leave CLI stderr empty when main() catches the error and runs
    ``print(f"error: {exc}", file=sys.stderr)``.
    """
    msg = "cfg.store ({s3}) differs from sidecar ({gcs})"
    err = SidecarMismatch(msg)
    assert str(err) == msg
