"""EphemeralIndex — store-backed pod-discovery seam for ``--ephemeral`` warm-reuse.

Records a minimal ``(pod_id, WAK, kinoforge_key, endpoint, provider,
created_at)`` row per pod provisioned under
:class:`~kinoforge.core.ephemeral.EphemeralSession`. Both
``_scan_warm_candidates`` (production warm-reuse path) and
``find_warm_attach_candidate`` (LoRA-flexible matcher, ``--dry-run-swap``
path) read this index so a second ``--ephemeral`` CLI invocation can
discover the surviving pod from the first.

Design: ``docs/superpowers/specs/2026-06-27-ephemeral-warm-reuse-discovery-design.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)

_INDEX_NAMESPACE = "_lifecycle"
_INDEX_FILENAME = "ephemeral-index.json"
_LOCK_KEY = "ephemeral-index/_lifecycle"
_LOCK_TTL_S = 30.0


@dataclass(frozen=True)
class EphemeralIndexRow:
    """One discoverable ephemeral pod.

    Frozen so a misbehaving caller cannot mutate a row after it has been
    handed to the matcher; the index re-reads from disk on every lookup.

    Attributes:
        id: Provider-side pod identifier.
        warm_attach_key: WAK hex string. Used by
            :func:`~kinoforge.core.warm_reuse.matcher.find_warm_attach_candidate`.
        kinoforge_key: 12-char ``cfg.capability_key().derive()`` prefix.
            Used by ``_scan_warm_candidates`` via the ledger-entry-shaped
            ``tags.kinoforge_key`` field.
        endpoints: Port -> URL mapping the orchestrator received at
            cold-create time. RunPod's ``get_instance`` does not return
            endpoints, so the warm-attach path replays this dict onto
            the resolved Instance to populate
            ``Instance.endpoints[<port>]`` for engine ``wait_for_ready``
            URL construction.
        provider: Provider kind string (``"runpod"``, ``"skypilot"``, ...)
            — disambiguates which backend to instantiate.
        created_at_local: ISO-format local-TZ timestamp; debugging +
            future sweeper TTL backstop.
    """

    id: str
    warm_attach_key: str
    kinoforge_key: str
    endpoints: dict[str, str]
    provider: str
    created_at_local: str

    def to_entry_dict(self) -> dict[str, Any]:
        """Return ledger-entry-shaped dict for matcher + scan consumers.

        Sparse on purpose: no ``status``, ``lora_inventory``,
        ``loras_dir_free_bytes``, or ``heartbeat_thread_tick`` — the
        matcher's existing ``always_reprobe`` path under ``--ephemeral``
        refills these on attach. Carrying stale snapshots would mislead
        the eligibility filter.

        ``endpoints`` is the canonical key consumed by
        ``_scan_warm_candidates`` to repopulate
        :attr:`Instance.endpoints` after ``provider.get_instance``
        returns a sparse Instance.
        """
        return {
            "id": self.id,
            "provider": self.provider,
            "endpoints": dict(self.endpoints),
            "warm_attach_key": self.warm_attach_key,
            "tags": {"kinoforge_key": self.kinoforge_key},
        }


class EphemeralIndex:
    """Locked RMW writer + lock-free reader for ``ephemeral-index.json``.

    Mirrors :class:`~kinoforge.core.lifecycle.Ledger`'s lock pattern:
    ``add`` / ``remove`` take the cross-process ``ephemeral-index/_lifecycle``
    lock; ``rows`` / ``rows_by_wak`` / ``rows_by_kinoforge_key`` are
    lock-free so the matcher hot path never contends with cleanup.

    Args:
        store: The :class:`~kinoforge.stores.base.ArtifactStore` to back
            the file. Typically the same store the ``Ledger`` uses.
        mutate_ttl_s: Cross-process lease duration for RMW operations.
            Default 30s — covers a single read-modify-write round-trip.
    """

    def __init__(
        self,
        store: ArtifactStore,
        *,
        mutate_ttl_s: float = _LOCK_TTL_S,
    ) -> None:
        """Initialise the index. See class docstring for argument semantics."""
        self._store = store
        self._mutate_ttl_s = mutate_ttl_s

    def _read_raw(self) -> list[dict[str, Any]]:
        uri = self._store.uri_for(_INDEX_NAMESPACE, _INDEX_FILENAME)
        try:
            data = self._store.get_json(uri)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning("ephemeral-index.json malformed (%s); treating as empty", exc)
            return []
        rows = data.get("rows", [])
        return [r for r in rows if isinstance(r, dict)]

    def _write_raw(self, rows: list[dict[str, Any]]) -> None:
        self._store.put_json(  # kinoforge:public-write
            _INDEX_NAMESPACE, _INDEX_FILENAME, {"rows": rows}
        )

    @staticmethod
    def _row_from_dict(d: dict[str, Any]) -> EphemeralIndexRow | None:
        try:
            endpoints_raw = d.get("endpoints", {})
            if not isinstance(endpoints_raw, dict):
                endpoints_raw = {}
            endpoints: dict[str, str] = {
                str(k): str(v) for k, v in endpoints_raw.items()
            }
            return EphemeralIndexRow(
                id=d["id"],
                warm_attach_key=d["warm_attach_key"],
                kinoforge_key=d["kinoforge_key"],
                endpoints=endpoints,
                provider=d["provider"],
                created_at_local=d["created_at_local"],
            )
        except KeyError as exc:
            _log.warning("ephemeral-index row missing field %s; skipping: %r", exc, d)
            return None

    def add(self, row: EphemeralIndexRow) -> None:
        """Insert or replace the row for ``row.id``.

        Idempotent on collision — second add of the same ``id`` overwrites
        the existing row's fields (not appends). Locked RMW so concurrent
        adds from two threads or two processes serialize cleanly.
        """
        with self._store.acquire_lock(_LOCK_KEY, ttl_s=self._mutate_ttl_s):
            rows = self._read_raw()
            new_rows = [r for r in rows if r.get("id") != row.id]
            new_rows.append(
                {
                    "id": row.id,
                    "warm_attach_key": row.warm_attach_key,
                    "kinoforge_key": row.kinoforge_key,
                    "endpoints": dict(row.endpoints),
                    "provider": row.provider,
                    "created_at_local": row.created_at_local,
                }
            )
            self._write_raw(new_rows)

    def remove(self, pod_id: str) -> None:
        """Remove the row for ``pod_id``. No-op if missing."""
        with self._store.acquire_lock(_LOCK_KEY, ttl_s=self._mutate_ttl_s):
            rows = self._read_raw()
            new_rows = [r for r in rows if r.get("id") != pod_id]
            if len(new_rows) != len(rows):
                self._write_raw(new_rows)

    def rows(self) -> list[EphemeralIndexRow]:
        """Return all rows. Lock-free."""
        return [
            r
            for r in (self._row_from_dict(d) for d in self._read_raw())
            if r is not None
        ]

    def rows_by_wak(self, wak_hex: str) -> list[EphemeralIndexRow]:
        """Return rows whose ``warm_attach_key`` matches ``wak_hex``. Lock-free."""
        return [r for r in self.rows() if r.warm_attach_key == wak_hex]

    def rows_by_kinoforge_key(self, cap_key12: str) -> list[EphemeralIndexRow]:
        """Return rows whose ``kinoforge_key`` matches ``cap_key12``. Lock-free."""
        return [r for r in self.rows() if r.kinoforge_key == cap_key12]
