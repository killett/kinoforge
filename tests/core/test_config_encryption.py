"""StoreEncryptionConfig + signed_url_default_ttl_s round-trip tests."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig, load_config


def test_default_encryption_is_provider_managed():
    cfg = StoreEncryptionConfig()
    assert cfg.mode == "default"
    assert cfg.kms_key_id is None


def test_kms_mode_requires_key_id():
    with pytest.raises(ValidationError) as excinfo:
        StoreEncryptionConfig(mode="kms")
    msg = str(excinfo.value)
    assert "encryption.mode='kms' requires encryption.kms_key_id" in msg


def test_kms_mode_with_key_id_constructs():
    cfg = StoreEncryptionConfig(
        mode="kms", kms_key_id="arn:aws:kms:us-east-1:1:key/abc"
    )
    assert cfg.mode == "kms"
    assert cfg.kms_key_id == "arn:aws:kms:us-east-1:1:key/abc"


def test_bogus_mode_rejected():
    with pytest.raises(ValidationError):
        StoreEncryptionConfig(mode="rot13")  # type: ignore[arg-type]


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        StoreEncryptionConfig(extra_field=1)  # type: ignore[call-arg]


def test_store_config_defaults():
    sc = StoreConfig()
    assert sc.encryption.mode == "default"
    assert sc.encryption.kms_key_id is None
    assert sc.signed_url_default_ttl_s == 3600


def test_yaml_round_trip_kms(tmp_path):
    doc = {
        "store": {
            "kind": "s3",
            "bucket": "demo",
            "encryption": {
                "mode": "kms",
                "kms_key_id": "arn:aws:kms:us-east-1:1:key/abc",
            },
            "signed_url_default_ttl_s": 600,
        },
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [{"kind": "base", "ref": "local://m", "target": "diffusion_models"}],
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(doc))
    cfg = load_config(str(p))
    assert cfg.store.encryption.mode == "kms"
    assert cfg.store.encryption.kms_key_id == "arn:aws:kms:us-east-1:1:key/abc"
    assert cfg.store.signed_url_default_ttl_s == 600
