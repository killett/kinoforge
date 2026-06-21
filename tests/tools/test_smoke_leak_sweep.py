"""smoke_leak_sweep — Layer-3 watchdog."""

from __future__ import annotations

import time

import pytest

import tools.smoke_leak_sweep as sweep


class _FakeInst:
    def __init__(
        self,
        id: str,
        created_h_ago: float,
        tag: str | None,  # noqa: A002
    ) -> None:
        self.id = id
        self.created_at = time.time() - created_h_ago * 3600
        self.tags = {"smoke_tier": tag} if tag else {}
        self.cost_rate_usd_per_hr = 1.79


def test_under_age_budget_no_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: tool destroys pods within budget."""
    destroyed: list[str] = []

    class _Prov:
        def list_instances(self) -> list[_FakeInst]:
            return [_FakeInst("ok-1", 0.1, "kinoforge-smoke-tier-3")]

        def destroy_instance(self, pid: str) -> None:
            destroyed.append(pid)

    monkeypatch.setattr(sweep, "_get_runpod_provider", lambda: _Prov())
    monkeypatch.setattr(sweep, "_post_issue", lambda **_: None)
    assert sweep.main([]) == 0
    assert destroyed == []


def test_over_tier3_budget_destroys_and_posts_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: tier-3 45-min ceiling not enforced."""
    destroyed: list[str] = []
    issues: list[dict[str, object]] = []

    class _Prov:
        def list_instances(self) -> list[_FakeInst]:
            return [
                _FakeInst("leak", 1.5, "kinoforge-smoke-tier-3"),
            ]

        def destroy_instance(self, pid: str) -> None:
            destroyed.append(pid)

    monkeypatch.setattr(sweep, "_get_runpod_provider", lambda: _Prov())
    monkeypatch.setattr(sweep, "_post_issue", lambda **kw: issues.append(kw))
    assert sweep.main([]) == 0
    assert destroyed == ["leak"]
    assert issues and issues[0]["pod_id"] == "leak"


def test_dry_run_skips_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    destroyed: list[str] = []

    class _Prov:
        def list_instances(self) -> list[_FakeInst]:
            return [
                _FakeInst("would-reap", 2.0, "kinoforge-smoke-tier-3"),
            ]

        def destroy_instance(self, pid: str) -> None:
            destroyed.append(pid)

    monkeypatch.setattr(sweep, "_get_runpod_provider", lambda: _Prov())
    monkeypatch.setattr(sweep, "_post_issue", lambda **_: None)
    assert sweep.main(["--dry-run"]) == 0
    assert destroyed == []


def test_untagged_pod_uses_default_4h_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destroyed: list[str] = []

    class _Prov:
        def list_instances(self) -> list[_FakeInst]:
            return [
                _FakeInst("recent-untagged", 2.0, None),
                _FakeInst("ancient-untagged", 5.0, None),
            ]

        def destroy_instance(self, pid: str) -> None:
            destroyed.append(pid)

    monkeypatch.setattr(sweep, "_get_runpod_provider", lambda: _Prov())
    monkeypatch.setattr(sweep, "_post_issue", lambda **_: None)
    assert sweep.main([]) == 0
    assert destroyed == ["ancient-untagged"]


def test_list_failure_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: tool swallows list_instances failure and exits 0 silently."""

    def _boom() -> object:
        raise RuntimeError("RunPod GraphQL unreachable")

    monkeypatch.setattr(sweep, "_get_runpod_provider", _boom)
    assert sweep.main([]) == 1
