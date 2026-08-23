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
`.aws/kms-test-key.arn` when that file exists. When neither is available,
the rendered policy drops the `KMSLayerW` statement instead of failing --
that statement only exists for Layer W CMEK bucket tests, a new operator
standing up SkyPilot does not need it, and the rest of the policy is still
valid and attachable without it.
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
# narrow ([A-Z0-9_] only) so the error message can name the exact
# survivor. Must include digits: <S3_BUCKET_PREFIX> itself contains one
# ("S3"), so the earlier [A-Z_]-only pattern did not match it -- a
# survived, unsubstituted <S3_BUCKET_PREFIX> would fall through to the
# blanket "<" backstop below instead of being named specifically here,
# which is correct as defence in depth but the wrong place to catch it
# first.
_PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")

_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")


_KMS_STATEMENT_SID = "KMSLayerW"


def render(
    policy_text: str, *, account: str, kms_key_id: str | None, bucket_prefix: str
) -> str:
    """Substitute every placeholder in *policy_text*.

    Args:
        policy_text: Raw contents of the tracked policy template.
        account: 12-digit AWS account id.
        kms_key_id: Bare KMS key UUID (not the full ARN), or `None` to
            render without the `KMSLayerW` statement -- the CMEK grant
            only Layer W's bucket tests need, not a bare SkyPilot launch.
            When `None`, a one-line notice naming the dropped statement
            and `--kms-key-id` as the way to get it back is printed to
            stderr.
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

    if kms_key_id is None:
        # No key available -- drop the KMSLayerW statement entirely rather
        # than leaving <KMS_KEY_ID> unsubstituted (which the survivor check
        # below would then correctly refuse to attach). This must happen at
        # the JSON level, not by string-matching the Sid line, so a
        # statement-shape change to the template doesn't silently corrupt
        # unrelated JSON around it.
        policy = json.loads(policy_text)
        statements = policy.get("Statement", [])
        kept = [s for s in statements if s.get("Sid") != _KMS_STATEMENT_SID]
        if len(kept) != len(statements):
            print(
                f"render_aws_policy: no KMS key id available -- dropping the "
                f"{_KMS_STATEMENT_SID} statement from the rendered policy "
                "(needed only for CMEK / Layer W bucket tests; pass "
                "--kms-key-id to include it)",
                file=sys.stderr,
            )
        policy["Statement"] = kept
        policy_text = json.dumps(policy)

    out = policy_text.replace("<AWS_ACCOUNT>", account).replace(
        "<S3_BUCKET_PREFIX>", bucket_prefix
    )
    if kms_key_id is not None:
        out = out.replace("<KMS_KEY_ID>", kms_key_id)
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
            f"{arn_file} is absent. Expected the gitignored .aws/kms-test-key.arn. "
            "Pass --kms-key-id directly, or omit both and the rendered policy "
            "will drop the KMSLayerW statement instead (fine for SkyPilot; "
            "needed only for CMEK / Layer W bucket tests)."
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
    `O_NOFOLLOW` refuses to follow a symlink at *out_path*. The mode
    argument to `os.open()` only applies when the call actually creates
    the file, though — if a *regular* file already exists at *out_path*
    (mode `0o666` in a world-writable directory like `/tmp` is a
    plausible pre-existing state, not just an attacker-planted one),
    `O_CREAT` opens it as-is and `O_NOFOLLOW` has nothing to refuse (it's
    not a symlink). Left uncovered, that regular-file case reaches the
    same predictable-`/tmp`-path exposure `O_NOFOLLOW` was added for, just
    via a pre-created file instead of a pre-created symlink. `os.fchmod()`
    right after `open()` closes that gap unconditionally, whether this
    call created the file or reused an existing one — no window where a
    concrete account id and KMS key id sit at a wider mode than `0o600`.

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
    os.fchmod(fd, 0o600)
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
        ValueError: The requested output path is inside the repository;
            the gitignored KMS ARN file exists but its contents are not a
            KMS key ARN (see `resolve_kms_key_id`'s `Raises` -- an absent
            file is NOT an error here, see below); or an input `render()`
            rejects (see `render`'s `Raises`).
        OSError: The output path is a symlink or otherwise cannot be
            opened for exclusive write — see `_write_secure`.

    Note:
        `--kms-key-id` omitted and `.aws/kms-test-key.arn` absent is NOT
        an error: it falls back to rendering without the `KMSLayerW`
        statement (see `render`'s *kms_key_id* behavior).
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
    kms_key_id: str | None = args.kms_key_id
    if not kms_key_id:
        try:
            kms_key_id = resolve_kms_key_id()
        except FileNotFoundError:
            # No explicit --kms-key-id and no bootstrapped ARN file -- fall
            # back to rendering without the KMSLayerW statement rather than
            # raising. A malformed (but present) ARN file still raises
            # ValueError from resolve_kms_key_id() and is NOT caught here:
            # that signals corrupted state, not "no key available".
            kms_key_id = None
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
