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
