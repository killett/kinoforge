"""C29 — FakeEngine.wait_for_ready honors CancelToken (Protocol parity)."""

from __future__ import annotations

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled
from kinoforge.core.interfaces import Instance
from kinoforge.engines.fake import FakeEngine


def _instance() -> Instance:
    return Instance(
        id="pod-fake",
        provider="fake",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://example.invalid:8000"},
    )


def test_fake_wait_for_ready_raises_cancelled_when_token_set() -> None:
    token = CancelToken()
    token.set()
    with pytest.raises(Cancelled):
        FakeEngine().wait_for_ready(
            _instance(),
            http_get=lambda _u: {},
            sleep=lambda _s: None,
            get_instance=lambda _id: _instance(),
            timeout_s=1.0,
            cancel_token=token,
        )


def test_fake_wait_for_ready_no_token_preserves_behavior() -> None:
    FakeEngine().wait_for_ready(
        _instance(),
        http_get=lambda _u: {},
        sleep=lambda _s: None,
        get_instance=lambda _id: _instance(),
        timeout_s=1.0,
    )
