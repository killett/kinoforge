"""Client-side knowledge of which diffusers server modules serve LoRAs.

Keyed by the dotted module named in ``engine.diffusers.server_cmd`` — the same
signal the provision renderer keys off.

This registry answers only what a controller can know WITHOUT a pod: does this
server serve LoRAs, and what is the target universe for its model family. It
cannot know which checkpoint partitions a given pod actually loaded, so the pod
narrows further at apply time. Config load catches wrong family / no support /
typo; the pod catches not-loaded-in-this-workflow.

``_REGISTRY`` is private — enumerate it via :func:`registered_modules`, not by
reaching into the dict directly.

Mirrored by each server's ``/health.lora`` block and locked by
``tests/engines/diffusers/test_lora_profile_parity.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClientLoraProfile:
    """What the controller knows about one server module's LoRA support."""

    supported: bool
    target_universe: tuple[str, ...]


_REGISTRY: dict[str, ClientLoraProfile] = {
    "kinoforge.engines.diffusers.servers.wan_t2v_server": ClientLoraProfile(
        supported=True, target_universe=("high_noise", "low_noise")
    ),
    "kinoforge.engines.diffusers.servers.minimax_h3_server": ClientLoraProfile(
        supported=True, target_universe=("transformer", "transformer_ref")
    ),
}


def client_profile_for_server_module(module: str) -> ClientLoraProfile | None:
    """Return the profile for *module*, or ``None`` when unregistered.

    Args:
        module: Dotted server module path from ``server_cmd``.

    Returns:
        The registered :class:`ClientLoraProfile`, else ``None``.
    """
    return _REGISTRY.get(module)


def registered_modules() -> tuple[str, ...]:
    """Return every dotted server module registered in the client registry.

    The supported way to enumerate the registry — do not read ``_REGISTRY``
    directly from outside this module.

    Returns:
        Registered module names, in registration order.
    """
    return tuple(_REGISTRY)


def server_module_from_cfg(cfg: object) -> str | None:
    """Extract the dotted server module from a cfg's ``server_cmd``.

    Recognises the ``python -m <module>`` shape every shipped diffusers
    config uses.

    Args:
        cfg: A loaded ``Config``.

    Returns:
        The dotted module, or ``None`` when the argv is not ``-m``-shaped.
    """
    engine = getattr(cfg, "engine", None)
    diffusers = getattr(engine, "diffusers", None) if engine is not None else None
    argv: list[str] = list(getattr(diffusers, "server_cmd", []) or [])
    if "-m" in argv:
        idx = argv.index("-m")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None
