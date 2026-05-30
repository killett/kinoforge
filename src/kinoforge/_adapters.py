"""Adapter self-registration hub.

This is the SOLE module in the kinoforge package that imports concrete adapter
implementations.  Every import here triggers the adapter's self-registration
call, making it visible to the registry under its declared name or scheme.

Core and the CLI MUST NOT import concrete adapters directly — they go through
the registry (``kinoforge.core.registry``).  This module is the one permitted
exception: it wires all adapters in one place so the rest of the codebase
stays agnostic of concrete implementations.

Usage::

    import kinoforge._adapters  # noqa: F401

Importing this module is side-effect-only; it registers every adapter and
exports nothing.
"""

# Providers
import kinoforge.engines.comfyui  # noqa: F401
import kinoforge.engines.diffusers  # noqa: F401

# Engines
import kinoforge.engines.fake  # noqa: F401
import kinoforge.engines.hosted  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.providers.runpod  # noqa: F401
import kinoforge.providers.skypilot  # noqa: F401
import kinoforge.sources.civitai  # noqa: F401

# Sources
import kinoforge.sources.http  # noqa: F401
import kinoforge.sources.huggingface  # noqa: F401

# Stores
import kinoforge.stores.local  # noqa: F401
