r"""Render `.aws/policies/skypilot-minimal.template.json` into an attachable policy.

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
import os
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_POLICY_PATH: Path = _REPO_ROOT / ".aws" / "policies" / "skypilot-minimal.template.json"
_KMS_ARN_FILE: Path = _REPO_ROOT / ".aws" / "kms-test-key.arn"

# Named placeholders this module knows how to substitute. Deliberately
# narrow ([A-Z_] only) so the error message can name the exact survivor.
_PLACEHOLDER_RE = re.compile(r"<[A-Z_]+>")

_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")


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
        ValueError: *bucket_prefix* is empty, `*`, or contains whitespace;
            *account* is not a 12-digit id; or a `<...>` placeholder
            survived substitution (named, if it matches the known
            `<[A-Z_]+>` shape, or unnamed otherwise).
        json.JSONDecodeError: The rendered text is not valid JSON — for
            example a substituted value contained an unescaped quote or
            backslash and corrupted the surrounding structure.
    """
    if not bucket_prefix:
        raise ValueError(
            "bucket_prefix must be non-empty; an empty prefix widens the S3 grant"
        )
    if bucket_prefix != bucket_prefix.strip() or any(
        ch.isspace() for ch in bucket_prefix
    ):
        raise ValueError(
            f"bucket_prefix {bucket_prefix!r} contains whitespace; that is "
            "never a valid S3 bucket-name-prefix character and likely "
            "indicates a copy-paste mistake"
        )
    if "*" in bucket_prefix:
        raise ValueError(
            f"bucket_prefix {bucket_prefix!r} contains '*'; a wildcard prefix "
            "widens the S3 grant to arn:aws:s3:::*-* -- far broader than the "
            "operator asked for"
        )
    if not _ACCOUNT_RE.fullmatch(account):
        raise ValueError(
            f"account {account!r} is not a 12-digit AWS account id; a "
            "malformed or wildcard account (e.g. '*') widens every ARN "
            "built from it"
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
    if "<" in out:
        raise ValueError(
            "a '<' survived rendering that the named-placeholder pattern "
            "<[A-Z_]+> did not match -- e.g. a placeholder with a digit, "
            "hyphen, or lowercase letter such as <S3_BUCKET_PREFIX_2> or "
            "<kms_key_id>. An IAM policy document has no legitimate use "
            "for '<'; fix the source template or teach render() about the "
            "new placeholder shape before attaching."
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


def _write_secure(out_path: Path, rendered: str) -> None:
    """Write *rendered* to *out_path* with no world-readable or symlink window.

    `write_text()` followed by a separate `chmod()` leaves the file at the
    umask-determined mode (typically `0o644`, world-readable) between
    creation and the chmod call, and `write_text()` follows a pre-existing
    symlink at *out_path* — so a predictable path in a world-writable
    directory (the documented `/tmp` usage) is a symlink-attack target.
    `O_NOFOLLOW` refuses to follow a symlink at *out_path*, and passing the
    mode to `os.open()` means the file is created at `0o600` atomically,
    with no window where a concrete account id and KMS key id sit
    world-readable on disk.

    Args:
        out_path: Path to write to, exactly as the caller requested it
            (NOT pre-resolved — resolving follows symlinks, which would
            erase the very information `O_NOFOLLOW` needs to detect a
            symlink at this exact path).
        rendered: Rendered policy JSON text.

    Raises:
        OSError: *out_path* already exists as a symlink (`ELOOP`) or
            otherwise cannot be opened for exclusive, non-following write.
    """
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(rendered)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code (always `0`; failures raise instead — see
        `Raises`). The `__main__` guard below is what converts a raise
        into a printed message and exit code `2` for an interactive
        caller; `main()` itself keeps raising so programmatic callers
        (including tests) see the real exception.

    Raises:
        ValueError: The requested output path is inside the repository,
            or an input `render()` rejects (see `render`'s `Raises`).
        FileNotFoundError: `--kms-key-id` was omitted and
            `.aws/kms-test-key.arn` is absent.
        OSError: The output path is a symlink or otherwise cannot be
            opened for exclusive write — see `_write_secure`.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default=None)
    parser.add_argument("--kms-key-id", default=None)
    parser.add_argument("--bucket-prefix", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    # Resolved path is for the containment check (and the printed message)
    # ONLY -- resolving follows symlinks, which is exactly the information
    # _write_secure's O_NOFOLLOW needs intact. The write itself opens the
    # original, unresolved `out_path` so a pre-existing symlink AT that
    # exact path is refused rather than transparently followed.
    out_path = Path(args.out)
    resolved_out_path = out_path.resolve()
    if resolved_out_path.is_relative_to(_REPO_ROOT):
        raise ValueError(
            f"refusing to write {resolved_out_path} inside the repository — a "
            "rendered policy holds concrete identifiers and must never become "
            "a tracked file. Use a path under /tmp."
        )

    account = args.account or _default_account()
    kms_key_id = args.kms_key_id or resolve_kms_key_id()
    rendered = render(
        _POLICY_PATH.read_text(),
        account=account,
        kms_key_id=kms_key_id,
        bucket_prefix=args.bucket_prefix,
    )
    _write_secure(out_path, rendered)
    print(f"rendered policy written to {resolved_out_path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    try:
        _exit_code = main()
    except (ValueError, FileNotFoundError, OSError) as _exc:
        print(f"render_aws_policy: {_exc}", file=sys.stderr)  # noqa: T201
        _exit_code = 2
    raise SystemExit(_exit_code)
