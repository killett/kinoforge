"""Tests for the AWS scoped-policy renderer.

The renderer exists because a tracked policy file cannot be both
placeholder-clean and directly attachable. Every guarantee below is one
half of that trade.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.render_aws_policy import render, resolve_kms_key_id

_ACCOUNT = "9" + "18273645" + "019"
_KEY_ID = "4b0dbe0c-" + "3a76-401a-" + "ac2e-" + "d0d949b9fa3e"

_TEMPLATE = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "IAMForSkyPilotRoles",
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": ["arn:aws:iam::<AWS_ACCOUNT>:role/skypilot-*"],
            },
            {
                "Sid": "S3KinoforgeBuckets",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": ["arn:aws:s3:::<S3_BUCKET_PREFIX>-*/*"],
            },
            {
                "Sid": "KMSLayerW",
                "Effect": "Allow",
                "Action": ["kms:Decrypt"],
                "Resource": ["arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/<KMS_KEY_ID>"],
            },
        ],
    }
)


def test_render_substitutes_all_three_placeholders() -> None:
    """The rendered policy is attachable: no placeholder survives.

    A bug that would fail this: substituting <AWS_ACCOUNT> only at its
    first occurrence, leaving the KMS ARN malformed and the attach 400ing
    with an unhelpful message.
    """
    out = render(
        _TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf-example"
    )
    assert "<" not in out
    parsed = json.loads(out)
    resources = [s["Resource"][0] for s in parsed["Statement"]]
    assert resources == [
        f"arn:aws:iam::{_ACCOUNT}:role/skypilot-*",
        "arn:aws:s3:::kf-example-*/*",
        f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/{_KEY_ID}",
    ]


def test_render_refuses_a_surviving_placeholder() -> None:
    """A half-rendered policy must never reach `aws iam put-user-policy`.

    A bug that would fail this: adding a fourth placeholder to the tracked
    template and forgetting to teach the renderer about it — AWS would
    then reject the attach with a malformed-ARN error that points nowhere
    near the actual cause.
    """
    template = _TEMPLATE.replace("skypilot-*", "<UNEXPECTED>-*")
    with pytest.raises(ValueError, match="<UNEXPECTED>"):
        render(
            template, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf-example"
        )


def test_render_rejects_an_empty_bucket_prefix() -> None:
    """An empty prefix silently widens the S3 grant to `arn:aws:s3:::-*`.

    A bug that would fail this: defaulting `bucket_prefix` to "" and
    producing a policy whose scope is not what the operator thinks.
    """
    with pytest.raises(ValueError, match="bucket_prefix"):
        render(_TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="")


def test_resolve_kms_key_id_reads_the_gitignored_arn_file(tmp_path: Path) -> None:
    """The key id comes from the untracked ARN file, never from the tree.

    Same file `tools/cloud_perms_probe.py:68` already reads.
    """
    arn_file = tmp_path / "kms-test-key.arn"
    arn_file.write_text(f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/{_KEY_ID}\n")
    assert resolve_kms_key_id(arn_file) == _KEY_ID


def test_resolve_kms_key_id_errors_with_a_remediation_hint(tmp_path: Path) -> None:
    """A missing ARN file must say what to do, not just what failed.

    A bug that would fail this: letting the bare FileNotFoundError escape,
    which tells a new operator nothing about `tools/bootstrap_kms.py`.
    """
    with pytest.raises(FileNotFoundError, match="kms-test-key.arn"):
        resolve_kms_key_id(tmp_path / "absent.arn")


def test_main_refuses_to_write_inside_the_repo(tmp_path: Path) -> None:
    """A rendered policy inside the tree becomes a tracked-file candidate.

    A bug that would fail this: accepting `--out .aws/policies/rendered.json`,
    which reintroduces exactly the concrete-identifier leak the sibling
    scrub test exists to stop.
    """
    from tools.render_aws_policy import main

    with pytest.raises(ValueError, match="inside the repository"):
        main(
            [
                "--account",
                _ACCOUNT,
                "--kms-key-id",
                _KEY_ID,
                "--bucket-prefix",
                "kf-example",
                "--out",
                str(Path(__file__).resolve().parents[2] / "rendered.json"),
            ]
        )
