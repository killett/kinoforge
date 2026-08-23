r"""Render `.aws/policies/skypilot-minimal.json` into an attachable policy.

The tracked policy carries `<AWS_ACCOUNT>`, `<KMS_KEY_ID>` and
`<S3_BUCKET_PREFIX>` placeholders, so it cannot be handed to
`aws iam put-user-policy` directly — AWS rejects a malformed ARN. This
module substitutes them and writes the result OUTSIDE the repository, which
is what lets the tracked file stay clean under
`tests/test_cloud_identifier_scrub.py` while the attach still works.

Usage::

    python tools/render_aws_policy.py \\
      --bucket-prefix my-prefix \\
      --out /tmp/skypilot-minimal.rendered.json

`--account` defaults to the caller's own account via `sts:GetCallerIdentity`;
`--kms-key-id` defaults to the key id parsed out of the gitignored
`.aws/kms-test-key.arn`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_POLICY_PATH: Path = _REPO_ROOT / ".aws" / "policies" / "skypilot-minimal.json"
_KMS_ARN_FILE: Path = _REPO_ROOT / ".aws" / "kms-test-key.arn"

_PLACEHOLDER_RE = re.compile(r"<[A-Z_]+>")


def render(
    policy_text: str, *, account: str, kms_key_id: str, bucket_prefix: str
) -> str:
    """Substitute every placeholder in *policy_text*.

    Args:
        policy_text: Raw contents of the tracked policy template.
        account: 12-digit AWS account id.
        kms_key_id: Bare KMS key UUID (not the full ARN).
        bucket_prefix: S3 bucket-name prefix the policy is scoped to.

    Returns:
        The rendered policy JSON as text.

    Raises:
        ValueError: *bucket_prefix* is empty, or a placeholder survived
            substitution.
    """
    if not bucket_prefix:
        raise ValueError(
            "bucket_prefix must be non-empty; an empty prefix widens the S3 grant"
        )

    out = (
        policy_text.replace("<AWS_ACCOUNT>", account)
        .replace("<KMS_KEY_ID>", kms_key_id)
        .replace("<S3_BUCKET_PREFIX>", bucket_prefix)
    )
    survivors = sorted(set(_PLACEHOLDER_RE.findall(out)))
    if survivors:
        raise ValueError(
            f"placeholder(s) survived rendering: {', '.join(survivors)}. "
            "Teach render() about them before attaching — AWS rejects a "
            "malformed ARN with an error that points nowhere near the cause."
        )
    json.loads(out)  # fail here, not at the AWS API boundary
    return out


def resolve_kms_key_id(arn_file: Path = _KMS_ARN_FILE) -> str:
    """Parse the bare key UUID out of the gitignored KMS ARN file.

    Args:
        arn_file: Path to the file holding the full KMS key ARN. Defaults
            to `.aws/kms-test-key.arn`, the same file
            `tools/cloud_perms_probe.py:68` reads.

    Returns:
        The bare key UUID.

    Raises:
        FileNotFoundError: The ARN file is absent.
        ValueError: The file contents are not a KMS key ARN.
    """
    if not arn_file.exists():
        raise FileNotFoundError(
            f"{arn_file} is absent. Expected the gitignored .aws/kms-test-key.arn; "
            "create it with `pixi run python tools/bootstrap_kms.py`, or pass "
            "--kms-key-id."
        )
    text = arn_file.read_text().strip()
    _, _, key_id = text.partition(":key/")
    if not key_id:
        raise ValueError(f"{arn_file} does not contain a KMS key ARN")
    return key_id


def _default_account() -> str:
    """Return the caller's AWS account id via STS.

    Returns:
        The 12-digit account id.
    """
    import boto3

    identity: dict[str, Any] = boto3.client("sts").get_caller_identity()
    return str(identity["Account"])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code.

    Raises:
        ValueError: The requested output path is inside the repository.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default=None)
    parser.add_argument("--kms-key-id", default=None)
    parser.add_argument("--bucket-prefix", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    out_path = Path(args.out).resolve()
    if out_path.is_relative_to(_REPO_ROOT):
        raise ValueError(
            f"refusing to write {out_path} inside the repository — a rendered "
            "policy holds concrete identifiers and must never become a tracked "
            "file. Use a path under /tmp."
        )

    account = args.account or _default_account()
    kms_key_id = args.kms_key_id or resolve_kms_key_id()
    rendered = render(
        _POLICY_PATH.read_text(),
        account=account,
        kms_key_id=kms_key_id,
        bucket_prefix=args.bucket_prefix,
    )
    out_path.write_text(rendered)
    out_path.chmod(0o600)
    print(f"rendered policy written to {out_path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
