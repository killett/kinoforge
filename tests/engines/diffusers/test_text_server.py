"""servers/text_server.py: mode derivation, gates, chat assembly, status schema.

Runs in the controller env (no torch) through the `_LOADER` seam; jobs are
executed by the server's REAL worker thread so the threading the pod uses is
what is under test.
"""

from __future__ import annotations

import ast
import importlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient


class _FakeProcessor:
    """Records the chat-template and encode calls; returns 4 prompt tokens."""

    def __init__(self, *, with_images: bool) -> None:
        self.image_processor = object() if with_images else None
        self.calls: list[tuple[str, Any, Any]] = []

    def apply_chat_template(
        self, messages: Any, *, add_generation_prompt: bool, tokenize: bool, **kw: Any
    ) -> str:
        assert add_generation_prompt is True
        assert tokenize is False
        self.calls.append(("template", messages, kw))
        return "TEMPLATE"

    def __call__(
        self, *, text: Any, images: Any = None, return_tensors: str
    ) -> dict[str, Any]:
        self.calls.append(("encode", text, images))
        return {"input_ids": np.zeros((1, 4), dtype=np.int64)}

    def decode(self, ids: Any, *, skip_special_tokens: bool) -> str:
        return f"fake completion of {len(ids)} tokens"


class _FakeModel:
    device = None

    def __init__(self) -> None:
        self.generate_kwargs: dict[str, Any] | None = None

    def generate(self, *, input_ids: Any, **kw: Any) -> Any:
        self.generate_kwargs = kw
        n_prompt = input_ids.shape[1]
        n_new = min(3, int(kw.get("max_new_tokens", 3)))
        return np.zeros((1, n_prompt + n_new), dtype=np.int64)


def _make_loader(*, with_images: bool, holder: dict[str, Any]) -> Callable[[str], Any]:
    def _load(model_id: str) -> Any:  # noqa: ANN401 -- srv.LoadedModel's type is unknown (dynamic module reload)
        srv = holder["srv"]
        proc = _FakeProcessor(with_images=with_images)
        model = _FakeModel()
        holder["proc"], holder["model"] = proc, model
        return srv.LoadedModel(
            model=model,
            processor=proc,
            supported_modes=srv.modes_for(proc),
            model_id=model_id,
        )

    return _load


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Reload the server with a fake loader; yield {srv, client, proc, model, upload_dir}."""
    monkeypatch.setenv("KINOFORGE_TEXT_MODEL_ID", "fake/model")
    monkeypatch.setenv("KINOFORGE_UPLOAD_DIR", str(tmp_path / "uploads"))
    from kinoforge.engines.diffusers.servers import text_server as srv

    srv = importlib.reload(srv)
    # The uploaded "PNG" bytes are not a real image; bypass PIL (pod-only) and
    # hand the chat assembly opaque tokens instead.
    monkeypatch.setattr(srv, "_open_images", lambda paths: [f"IMG:{p}" for p in paths])
    holder: dict[str, Any] = {"srv": srv, "upload_dir": tmp_path / "uploads"}
    holder["set_loader"] = lambda with_images: monkeypatch.setattr(
        srv, "_LOADER", _make_loader(with_images=with_images, holder=holder)
    )
    yield holder
    srv.jobs.clear()


def _start(server: dict[str, Any], *, with_images: bool) -> TestClient:
    server["set_loader"](with_images)
    client = TestClient(server["srv"].app)
    client.__enter__()  # runs the startup hook: loader + worker thread + ready
    server["client"] = client
    return client


def _wait_done(
    client: TestClient, job_id: str, timeout_s: float = 5.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = client.get(f"/text/status/{job_id}").json()
        if payload["state"] in {"done", "error"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _upload_png(client: TestClient, name: str = "ab12cd34.png") -> str:
    resp = client.put(
        "/upload",
        content=b"\x89PNG fake",
        headers={"Content-Type": "image/png", "X-Filename": name},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["path"])


def test_modes_are_derived_from_the_processor(server: dict[str, Any]) -> None:
    """Bug caught: the server echoing the config's declaration instead of
    deriving — the whole point of the pod-side half of the gate (§4.2)."""
    srv = server["srv"]
    assert srv.modes_for(_FakeProcessor(with_images=False)) == ("t2t",)
    assert srv.modes_for(_FakeProcessor(with_images=True)) == ("t2t", "it2t")


def test_health_reports_modes_and_the_closed_capability_vocabulary(
    server: dict[str, Any],
) -> None:
    """Bug caught: capabilities drifting from the hand-transcribed ADVERTISABLE_STAGES."""
    client = _start(server, with_images=True)
    payload = client.get("/health").json()
    assert payload["ready"] is True
    assert payload["model"] == "fake/model"
    assert payload["supported_modes"] == ["t2t", "it2t"]
    assert payload["capabilities"] == ["text", "upload"]
    assert payload["default_max_new_tokens"] == 256


def test_images_against_a_text_only_model_are_400_before_queueing(
    server: dict[str, Any],
) -> None:
    """Bug caught: a 400 raised from the worker thread, after the queue."""
    client = _start(server, with_images=False)
    path = _upload_png(client)
    resp = client.post("/text", json={"prompt": "describe", "images": [path]})
    assert resp.status_code == 400
    assert "no image path" in resp.json()["detail"]
    assert server["srv"].jobs == {}


def test_image_paths_outside_the_upload_dir_are_400(
    server: dict[str, Any], tmp_path: Path
) -> None:
    """Bug caught: /text reading an arbitrary pod file as an image, and a 400
    raised from inside the worker thread after the job was already enqueued
    (same bug class as the text-only-model gate above)."""
    client = _start(server, with_images=True)
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"x")
    resp = client.post("/text", json={"prompt": "p", "images": [str(outside)]})
    assert resp.status_code == 400
    assert server["srv"].jobs == {}


def test_t2t_job_runs_to_done_with_the_submit_and_poll_schema(
    server: dict[str, Any],
) -> None:
    """Bug caught: a `status`/`filename` (generate-style) payload the
    submit_and_poll client cannot read; usage counts wrong."""
    client = _start(server, with_images=False)
    job_id = client.post(
        "/text", json={"prompt": "hello", "params": {"max_new_tokens": 8}}
    ).json()["job_id"]
    payload = _wait_done(client, job_id)
    assert payload["state"] == "done"
    result = payload["result"]
    assert result["text"] == "fake completion of 3 tokens"
    assert result["finish_reason"] == "stop"
    assert result["usage"] == {"prompt_tokens": 4, "completion_tokens": 3}
    assert result["model"] == "fake/model"
    assert result["max_new_tokens"] == 8


def test_filling_max_new_tokens_reports_length(server: dict[str, Any]) -> None:
    """Bug caught: finish_reason always 'stop', hiding truncated answers."""
    client = _start(server, with_images=False)
    job_id = client.post(
        "/text", json={"prompt": "hello", "params": {"max_new_tokens": 3}}
    ).json()["job_id"]
    assert _wait_done(client, job_id)["result"]["finish_reason"] == "length"


def test_t2t_chat_is_a_plain_string_user_turn(server: dict[str, Any]) -> None:
    """Bug caught: list-shaped content handed to a text tokenizer's template,
    which renders the Python repr of the list into the prompt."""
    client = _start(server, with_images=False)
    job_id = client.post("/text", json={"prompt": "hello"}).json()["job_id"]
    _wait_done(client, job_id)
    kind, messages, _ = server["proc"].calls[0]
    assert kind == "template"
    assert messages == [{"role": "user", "content": "hello"}]


def test_it2t_chat_interleaves_images_before_text_and_routes_kwargs(
    server: dict[str, Any],
) -> None:
    """Bug caught: chat_template_kwargs leaking into generate() (TypeError on
    the pod), or the system turn appearing when none was given."""
    client = _start(server, with_images=True)
    p1, p2 = _upload_png(client, "11111111.png"), _upload_png(client, "22222222.png")
    body = {
        "prompt": "compare",
        "system": "Be terse.",
        "images": [p1, p2],
        "params": {
            "max_new_tokens": 5,
            "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    }
    job_id = client.post("/text", json=body).json()["job_id"]
    _wait_done(client, job_id)
    _, messages, template_kwargs = server["proc"].calls[0]
    assert messages[0] == {"role": "system", "content": "Be terse."}
    user = messages[1]["content"]
    assert [c["type"] for c in user] == ["image", "image", "text"]
    assert user[-1]["text"] == "compare"
    assert template_kwargs == {"enable_thinking": False}
    _, text, images = server["proc"].calls[1]
    assert text == ["TEMPLATE"]
    assert len(images) == 2
    assert server["model"].generate_kwargs == {"max_new_tokens": 5, "temperature": 0.2}


def test_unknown_job_is_404(server: dict[str, Any]) -> None:
    client = _start(server, with_images=False)
    assert client.get("/text/status/nope").status_code == 404


def test_module_imports_without_torch() -> None:
    """Bug caught (U66's shape): a module-level torch/transformers import that
    only a pod can satisfy. The controller env has neither, so a successful
    import here IS the check; the sys.modules assertions make it explicit that
    nothing pulled them in as a side effect. PIL IS installed in the
    controller env (unlike torch/transformers) so it is deliberately NOT
    asserted absent here — ``test_servers_module_scope_has_no_torch_transformers_pil``
    below covers the PIL-stays-function-local rule via AST instead, which is
    import-order-independent."""
    import importlib
    import sys

    srv = importlib.import_module("kinoforge.engines.diffusers.servers.text_server")
    assert hasattr(srv, "app")
    for forbidden in ("torch", "transformers"):
        assert forbidden not in sys.modules, f"{forbidden} imported at module scope"


def test_servers_module_scope_has_no_torch_transformers_pil() -> None:
    """Bug caught: a module-level ``import torch`` / ``transformers`` / ``PIL``
    statement added to a pod-side server. Unlike the sys.modules check above
    (which is order-dependent: PIL may already be imported by an unrelated
    test collected earlier in the session, and torch/transformers being
    absent from sys.modules does not prove the module did not try — it only
    proves this particular test run never triggered the import), this parses
    the AST of each pod-side module directly and inspects only statements at
    MODULE scope — a `def` body's `import torch` is fine (and required, since
    torch is pod-only and these modules are unit-tested in a torch-less
    controller env); one hanging directly off the module `Module` node is the
    defect this guard exists to catch, independent of what else the test
    session happened to import first.
    """
    repo_root = Path(__file__).resolve().parents[3]
    forbidden_roots = {"torch", "transformers", "PIL"}
    for rel in (
        "src/kinoforge/engines/diffusers/servers/text_server.py",
        "src/kinoforge/engines/diffusers/servers/_upload.py",
    ):
        path = repo_root / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module-level statements ONLY, not nested
            if isinstance(node, ast.Import):
                hit = {a.name.split(".")[0] for a in node.names} & forbidden_roots
                assert not hit, (
                    f"{rel}: module-level `import` of {hit} at line {node.lineno}"
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in forbidden_roots, (
                    f"{rel}: module-level `from {node.module} import ...` at line {node.lineno}"
                )
