"""Behavior: compute.backend_options is namespaced and validated by its owner.

The whole point of the compute-seam rework: a field set for the wrong provider
must be loud. A typo'd key inside a namespace, or a namespace for a provider
that does not exist, is a misconfiguration the operator should learn about at
load time rather than from an invoice.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ConfigError


def _cfg(backend_options: dict[str, dict[str, Any]]) -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp16"},
            "spec": {"model": "m", "precision": "bf16"},
            "models": [
                {"kind": "base", "ref": "hf:org/repo", "target": "diffusion_models"}
            ],
            "compute": {
                "provider": "runpod",
                "image": "img:tag",
                "backend_options": backend_options,
            },
        }
    )


def test_known_key_in_the_selected_namespace_loads():
    cfg = _cfg({"runpod": {"cloud_type": "secure"}})
    assert cfg.backend_options_for("runpod").cloud_type == "secure"


def test_unknown_key_in_a_namespace_is_rejected_and_names_the_key():
    # Bug caught: `cloudtype: secure` silently does nothing and the pod lands
    # on a community host that deletes it mid-run (2026-07-03).
    with pytest.raises(ConfigError) as exc:
        _cfg({"runpod": {"cloudtype": "secure"}})
    assert "cloudtype" in str(exc.value)
    assert "cloud_type" in str(exc.value)  # names the accepted keys


def test_unknown_provider_namespace_is_rejected():
    # Bug caught: `runpid:` reads as "options for a provider I did not select"
    # and is discarded, which is precisely the F5 failure mode.
    with pytest.raises(ConfigError) as exc:
        _cfg({"runpid": {"cloud_type": "secure"}})
    assert "runpid" in str(exc.value)


def test_a_namespace_for_an_unselected_provider_still_validates_its_shape():
    # Bug caught: the typo hides in the block you are not currently running.
    with pytest.raises(ConfigError):
        _cfg({"skypilot": {"cluods": ["lambda"]}})


def test_absent_namespace_yields_defaults_not_none():
    cfg = _cfg({})
    assert cfg.backend_options_for("runpod").cloud_type == "any"


def test_options_models_forbid_extra_on_every_registered_provider():
    from kinoforge.core import registry

    for name in registry.provider_names():
        cls = registry.provider_class(name)
        assert cls is not None and hasattr(cls, "Options"), name
        assert cls.Options.model_config.get("extra") == "forbid", name
