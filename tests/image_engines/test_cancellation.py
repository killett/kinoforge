"""ImageBackend.result must honour a CancelToken.

Without this the CLI's two-press SIGINT handler is inert for the whole of a
~125 s Luma poll (successful-generations.md 15), even though
RemoteSubmitPollBackend underneath already supports cancellation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled
from kinoforge.core.interfaces import Artifact, ImageBackend, ImageJob


@dataclass
class _CountingBackend(ImageBackend):
    """ImageBackend whose poll count is observable.

    Mimics fal's hand-rolled loop: N iterations, token checked at the top of
    each, injected sleep between.
    """

    max_polls: int = 10
    polls: list[int] = field(default_factory=list)

    def capabilities(self) -> Any:  # pragma: no cover - not under test
        raise NotImplementedError

    def inspect_capabilities(self) -> Any:  # pragma: no cover - not under test
        raise NotImplementedError

    def submit(self, job: ImageJob) -> str:
        return "job-1"

    def result(
        self, job_id: str, *, cancel_token: CancelToken | None = None
    ) -> Artifact:
        from kinoforge.core.cancel import _NULL_TOKEN

        token = cancel_token if cancel_token is not None else _NULL_TOKEN
        for i in range(self.max_polls):
            token.raise_if_set()
            self.polls.append(i)
            # Only simulate a sibling cancelling mid-poll when a REAL token
            # was supplied. Calling .set() unconditionally here would mutate
            # the shared `_NULL_TOKEN` process-wide singleton on the no-token
            # path, permanently "cancelling" every other backend's default
            # fallback for the rest of this pytest session (collection order
            # runs this file before test_fal.py / test_luma_agents.py /
            # test_replicate.py / test_fake.py, so the corruption would
            # cascade into all of them).
            if cancel_token is not None and i == 2:
                token.set()
        return Artifact(filename="x.png")

    def endpoints(self) -> dict[str, str]:
        return {}


def test_abc_signature_accepts_a_cancel_token() -> None:
    """The ABC must declare the keyword, or no caller can pass one.

    Bug this catches: fixing cancellation inside the engines only, leaving the
    ABC narrow so KeyframeStage and generate_image still cannot pass a token —
    which is the defect exactly as it ships today.
    """
    import inspect

    sig = inspect.signature(ImageBackend.result)
    assert "cancel_token" in sig.parameters
    assert sig.parameters["cancel_token"].default is None


def test_token_tripped_mid_poll_stops_the_loop_early() -> None:
    """Assert the POLL COUNT, not just that Cancelled was raised.

    Bug this catches: a loop that runs all max_polls iterations and only checks
    the token at the end. `pytest.raises(Cancelled)` alone would pass against
    that, which is why the count is the assertion.
    """
    backend = _CountingBackend(max_polls=10)
    token = CancelToken()
    with pytest.raises(Cancelled):
        backend.result("job-1", cancel_token=token)
    assert backend.polls == [0, 1, 2], (
        f"expected the loop to stop on the iteration after the token was set; "
        f"got {len(backend.polls)} polls — the token is being checked too late"
    )


def test_no_token_means_no_behaviour_change() -> None:
    """The default keeps every existing caller source- and behaviour-compatible.

    Bug this catches: making cancel_token required, or defaulting it to a live
    token, either of which breaks KeyframeStage and all four engines' tests.
    """
    backend = _CountingBackend(max_polls=4)
    artifact = backend.result("job-1")
    assert artifact.filename == "x.png"
    assert len(backend.polls) == 4


@pytest.mark.parametrize(
    "module_name",
    [
        "kinoforge.image_engines.fal",
        "kinoforge.image_engines.luma_agents",
        "kinoforge.image_engines.replicate",
        "kinoforge.image_engines.fake",
    ],
)
def test_every_shipped_backend_accepts_the_keyword(module_name: str) -> None:
    """All four engines must take the keyword, or one silently cannot cancel.

    Bug this catches: wiring luma/replicate (easy, they subclass the capable
    backend) and forgetting fal's hand-rolled loop — which is the one engine the
    keyframe path actually used live.
    """
    import importlib
    import inspect

    module = importlib.import_module(module_name)
    backends = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, ImageBackend)
        and obj is not ImageBackend
    ]
    assert backends, f"no ImageBackend subclass found in {module_name}"
    for backend_cls in backends:
        sig = inspect.signature(backend_cls.result)
        assert "cancel_token" in sig.parameters, (
            f"{module_name}.{backend_cls.__name__}.result does not accept cancel_token"
        )
