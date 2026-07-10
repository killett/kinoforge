"""Behavior: the generalized BSA wheel builder renders a cp313 provision script."""

import importlib


def _mod():
    return importlib.import_module("tools.build_bsa_wheel")


def test_target_tag_is_cp313():
    m = _mod()
    assert m._GH_TAG == "bsa-cu124-torch2.6-cp313-v1"


def test_provision_script_builds_under_py313():
    m = _mod()
    script = m._build_provision_script(release_id=999)
    assert "3.13" in script
    assert "torch==2.6.0" in script
    assert "torchvision==0.21.0" in script
    assert "3453bbb1" in script
    assert "8.0;8.6;8.9;9.0" in script
    assert "pip wheel" in script


def test_torch_index_is_cu124():
    m = _mod()
    assert "cu124" in m._TORCH_INDEX


def test_main_loads_dotenv_before_reading_creds(monkeypatch):
    # Bug caught: `pixi run` does not auto-source .env, so without an explicit
    # load_env_file() the builder's own process lacks RUNPOD_API_KEY -> the
    # RunPod provider uses its no-User-Agent fallback seams -> Cloudflare 403.
    m = _mod()
    calls = []
    monkeypatch.setattr(m, "load_env_file", lambda *a, **k: calls.append("loaded"))
    monkeypatch.setattr(m, "_get_release_id", lambda _tok: 12345)
    monkeypatch.setattr(m.sys, "argv", ["build_bsa_wheel", "--dry-run"])
    monkeypatch.setenv("GH_TOKEN", "dummy-token-for-dry-run")

    rc = m.main()

    assert rc == 0
    assert calls == ["loaded"], "main() must call load_env_file() at startup"
