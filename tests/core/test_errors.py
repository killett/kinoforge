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
    """Same wiring contract for SidecarMigrationBlocked."""
    assert issubclass(SidecarMigrationBlocked, KinoforgeError)


def test_sidecar_mismatch_carries_message():
    """Exception message round-trips so the CLI can print it verbatim."""
    err = SidecarMismatch("cfg.store ({s3}) differs from sidecar ({gcs})")
    assert "differs" in str(err)
