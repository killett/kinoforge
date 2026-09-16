"""Behavior: RunPod ``mode: serverless`` is refused before it can fail obscurely.

U46. The serverless create is invalid GraphQL — it declares ``EndpointInput!``
and calls ``saveTemplate``, which takes ``SaveTemplateInput`` — so RunPod
answers ``GRAPHQL_VALIDATION_FAILED`` as a raw **HTTP 400**. That is precisely
the shape ``CLAUDE.md`` warns reads as "RunPod is down" rather than "your
request is malformed", and the operator would go hunting an outage.

It is also not one bug behind a working feature. Nothing in kinoforge can
consume a serverless endpoint: there is no worker handler in ``src/``, no
``runpod`` SDK dependency, and ``_create_serverless`` returns an ``Instance``
with no endpoints at all. A RunPod serverless worker polls RunPod's job queue
through a handler, while every kinoforge engine is an HTTP server reached via
the POD proxy. So the honest thing is to say so, loudly, at the point the
operator asks for it.
"""

from __future__ import annotations

from typing import Any

import pytest

import kinoforge.validation.checks  # noqa: F401 — self-registers the built-ins
from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import ValidationError
from kinoforge.validation import validate_for_generate, validate_for_load

# The built-ins self-register through a function-local import inside
# ``load_config``, so a module that builds a Config directly can otherwise run
# against an EMPTY registry — where every ``report.ok`` assertion below passes
# for the wrong reason. Importing the package at module scope makes the
# registration explicit and the negative tests load-bearing.

_BASE: dict[str, Any] = {
    "engine": {"kind": "diffusers", "precision": "fp16"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo", "target": "diffusion_models"}],
}

_SHIPPED = "examples/configs/runpod-diffusers-serverless.yaml"


def _cfg(compute: dict[str, Any]) -> Config:
    return Config.model_validate({**_BASE, "compute": compute})


def test_runpod_serverless_is_refused_at_generate_preflight() -> None:
    """The refusal names the mode, the provider and why it cannot work.

    Bug caught: without this, ``kinoforge generate`` on a serverless config
    loads clean, books nothing, and dies on a raw HTTP 400 from a malformed
    mutation — indistinguishable from a RunPod outage, which the project's own
    infra notes record as a costly misdiagnosis.
    """
    cfg = _cfg({"provider": "runpod", "mode": "serverless", "image": "img:1"})

    with pytest.raises(ValidationError) as exc:
        validate_for_generate(cfg)

    text = str(exc.value)
    assert "serverless" in text
    assert "runpod" in text.lower()
    # A refusal that does not say what to do instead is a dead end.
    assert "pod" in text


def test_the_shipped_serverless_config_still_LOADS() -> None:
    """Loading must stay green so the config stays inside the ratchet.

    Bug caught, and it is the reason this check is PREFLIGHT rather than
    STATIC: a STATIC ERROR rejects ``load_config`` itself, which would break
    ``tools/snapshot_launch_payloads.capture_payload`` and force the config
    back into ``EXCLUDED_CONFIGS`` — silently undoing U35 and taking the only
    serverless wire kinoforge has back out of the launch-payload ratchet.
    Refusing the operator and freezing the wire are different jobs.
    """
    cfg = load_config(_SHIPPED)

    assert cfg.compute is not None
    assert cfg.compute.mode == "serverless"
    # The load-context validator must also stay quiet, for the same reason.
    assert validate_for_load(cfg).ok


def test_runpod_pod_mode_is_untouched() -> None:
    """The overwhelming majority of configs must not notice this check.

    Bug caught: a check keyed on provider alone, or one that treats an unset
    mode as suspicious, refuses every RunPod config in the repo.
    """
    cfg = _cfg({"provider": "runpod", "mode": "pod", "image": "img:1"})

    assert validate_for_generate(cfg).ok


def test_a_modal_config_is_not_refused_for_the_word_serverless() -> None:
    """``Offer.mode`` and ``cfg.compute.mode`` are different fields.

    Bug caught: Modal's whole catalog is built from offers carrying
    ``mode="serverless"`` (``providers/modal/_catalog.py``), so a check that
    keys on the string "serverless" anywhere near the compute block — or on the
    provider's offers rather than the operator's config — refuses Modal work
    that is completely fine. This is the third over-blocking guard needed in
    two days, after U43 called a real GPU a typo and U44 nearly dropped the
    CUDA-capable Tesla V100s.
    """
    cfg = _cfg({"provider": "modal", "image": "img:1"})

    assert validate_for_generate(cfg).ok
