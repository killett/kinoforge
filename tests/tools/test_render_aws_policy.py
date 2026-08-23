"""Tests for the AWS scoped-policy renderer.

The renderer exists because a tracked policy file cannot be both
placeholder-clean and directly attachable. Every guarantee below is one
half of that trade.
"""

from __future__ import annotations

import json
import stat
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
    which tells a new operator nothing about `tools/bootstrap_kms.py`. A
    narrower bug that would still pass a canonical-name-only check:
    hardcoding the canonical filename and dropping the actual *arn_file*
    argument from the message, which would misdirect an operator who
    passed a custom, non-default path.
    """
    absent = tmp_path / "absent.arn"
    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_kms_key_id(absent)
    message = str(exc_info.value)
    assert "kms-test-key.arn" in message
    assert str(absent) in message


def test_render_refuses_an_unnamed_placeholder_shape() -> None:
    """The named-placeholder regex is deliberately narrow (`[A-Z_]` only);
    a placeholder with a digit, hyphen, or lowercase letter must still be
    caught, not silently rendered into legal-looking JSON.

    A bug that would fail this: only checking `_PLACEHOLDER_RE` survivors
    and returning `out` unconditionally otherwise. `<kms_key_id>` is legal
    JSON string content and `json.loads()` would not object either, so
    without a blanket `<` check the malformed ARN reaches
    `put-user-policy` exactly as this module exists to prevent.
    """
    template = _TEMPLATE.replace("<KMS_KEY_ID>", "<kms_key_id>")
    with pytest.raises(ValueError, match="'<' survived rendering"):
        render(
            template, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf-example"
        )


def test_render_rejects_a_non_12_digit_account() -> None:
    """A malformed or wildcard account widens every ARN built from it.

    A bug that would fail this: accepting `account="*"` and silently
    producing `arn:aws:iam::*:role/skypilot-*` -- a policy far wider than
    the operator believes, reached by a different input than the
    empty-`bucket_prefix` case.
    """
    with pytest.raises(ValueError, match="account"):
        render(_TEMPLATE, account="*", kms_key_id=_KEY_ID, bucket_prefix="kf-example")


def test_render_rejects_a_wildcard_bucket_prefix() -> None:
    """`bucket_prefix="*"` yields `arn:aws:s3:::*-*`, not the scope the
    operator believes they asked for.

    Reached by a different input than
    `test_render_rejects_an_empty_bucket_prefix`, which only covers the
    empty-string case.
    """
    with pytest.raises(ValueError, match="bucket_prefix"):
        render(_TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="*")


def test_render_rejects_a_bucket_prefix_with_whitespace() -> None:
    """Whitespace is never valid in an S3 bucket-name prefix and signals a
    copy-paste mistake, not operator intent.
    """
    with pytest.raises(ValueError, match="bucket_prefix"):
        render(
            _TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf example"
        )


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


def test_main_refuses_a_dot_dot_relative_path_into_the_repo() -> None:
    """`../` traversal must not bypass the in-repo-output guard.

    A bug that would fail this: comparing `args.out` textually instead of
    calling `.resolve()` first, so a path that reads as "elsewhere"
    syntactically actually normalizes to somewhere inside the repo.
    """
    from tools.render_aws_policy import main

    repo_root = Path(__file__).resolve().parents[2]
    traversal_out = str(repo_root / "tests" / ".." / "rendered.json")

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
                traversal_out,
            ]
        )


def test_main_refuses_a_symlinked_path_into_the_repo(tmp_path: Path) -> None:
    """A symlink whose link path is outside the repo but whose target is
    inside it must not bypass the in-repo-output guard.

    A bug that would fail this: resolving symlinks with anything other
    than `Path.resolve()` (or not at all), so `<outside>/link/rendered.json`
    reads as "outside" even though `link` points straight back into the
    repo root.
    """
    from tools.render_aws_policy import main

    repo_root = Path(__file__).resolve().parents[2]
    link = tmp_path / "link-into-repo"
    link.symlink_to(repo_root, target_is_directory=True)

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
                str(link / "rendered.json"),
            ]
        )


def test_main_refuses_to_follow_a_pre_existing_symlink_at_out(
    tmp_path: Path,
) -> None:
    """`--out` pointing at a pre-existing symlink must not be followed.

    The symlink and its target both live outside the repo, so the
    containment guard does not fire and this exercises the write path
    itself. A bug that would fail this: `write_text()` (or any open
    without `O_NOFOLLOW`) follows a pre-existing symlink at the target
    path -- a predictable filename in a world-writable directory like
    `/tmp` (the module's own documented usage example) is a symlink-attack
    target. Concrete account id and KMS key id would land in the
    attacker's file, which the old `chmod(0o600)` would then have made
    *more* private on the attacker's behalf.
    """
    from tools.render_aws_policy import main

    attacker_target = tmp_path / "attacker-owned-file"
    attacker_target.write_text("do not overwrite me via a followed symlink\n")
    out_link = tmp_path / "predictable-name.json"
    out_link.symlink_to(attacker_target)

    with pytest.raises(OSError):
        main(
            [
                "--account",
                _ACCOUNT,
                "--kms-key-id",
                _KEY_ID,
                "--bucket-prefix",
                "kf-example",
                "--out",
                str(out_link),
            ]
        )
    assert attacker_target.read_text() == "do not overwrite me via a followed symlink\n"


def test_render_drops_kms_statement_when_no_key_available() -> None:
    """No KMS key id -> a narrower, still-valid policy, not a hard failure.

    The `KMSLayerW` statement only exists for Layer W CMEK bucket tests; a
    new operator standing up SkyPilot does not need it, and forcing them to
    provision a KMS key first -- through a broken bootstrap tool, with
    permissions a bare new IAM user does not have -- to obtain a policy
    they can attach is backwards. `kms_key_id=None` must drop exactly the
    `KMSLayerW` statement and still produce valid, placeholder-free JSON.

    A bug that would fail this: leaving `<KMS_KEY_ID>` unsubstituted when
    the statement isn't dropped (the old behavior), which the placeholder
    survivor check would catch -- or dropping the wrong statement (e.g. by
    index instead of by Sid), which would silently narrow an unrelated
    grant instead.
    """
    out = render(
        _TEMPLATE, account=_ACCOUNT, kms_key_id=None, bucket_prefix="kf-example"
    )
    assert "<" not in out
    parsed = json.loads(out)
    sids = [s["Sid"] for s in parsed["Statement"]]
    assert "KMSLayerW" not in sids
    assert sids == ["IAMForSkyPilotRoles", "S3KinoforgeBuckets"]


def test_render_keeps_kms_statement_substituted_when_key_supplied() -> None:
    """Passing a real key id leaves behavior unchanged: statement present.

    This is the "did the KMS-optional change regress the existing path"
    check -- a bug that would fail this: making `kms_key_id=None` the
    silent default behavior even when a real id is supplied, dropping the
    statement operators who DO want CMEK / Layer W coverage still need.
    """
    out = render(
        _TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf-example"
    )
    parsed = json.loads(out)
    kms_statements = [s for s in parsed["Statement"] if s["Sid"] == "KMSLayerW"]
    assert len(kms_statements) == 1
    assert kms_statements[0]["Resource"] == [
        f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/{_KEY_ID}"
    ]


def test_render_warns_on_stderr_when_dropping_the_kms_statement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Silence here would be worse than the error it replaces.

    An operator who actually needed CMEK coverage and didn't notice it was
    silently dropped would find out only when a real bucket operation
    403s, far from the render step that caused it. A bug that would fail
    this: dropping the statement without printing anything, or printing a
    message that doesn't name `--kms-key-id` as the way to get it back.
    """
    render(_TEMPLATE, account=_ACCOUNT, kms_key_id=None, bucket_prefix="kf-example")
    stderr = capsys.readouterr().err
    assert "KMSLayerW" in stderr
    assert "--kms-key-id" in stderr


def test_render_does_not_warn_on_stderr_when_key_supplied(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The stderr notice is specific to the drop path, not printed always.

    A bug that would fail this: printing the drop notice unconditionally,
    which would train operators to ignore it -- exactly the kind of
    always-on noise that makes a real drop easy to miss.
    """
    render(_TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf-example")
    stderr = capsys.readouterr().err
    assert stderr == ""


def test_main_falls_back_to_no_kms_when_arn_file_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI path this task exists to fix: a first-time operator with no
    `--kms-key-id` and no `.aws/kms-test-key.arn` must get a rendered,
    attachable policy -- not a `FileNotFoundError` pointing at a broken
    bootstrap tool the bare new IAM user has no permissions to run.

    A bug that would fail this: `main()` still calling
    `resolve_kms_key_id()` and letting `FileNotFoundError` propagate
    instead of catching it and falling back to `kms_key_id=None`.

    Monkeypatches `resolve_kms_key_id` itself rather than the module-level
    `_KMS_ARN_FILE` constant: `resolve_kms_key_id`'s `arn_file` parameter
    defaults to `_KMS_ARN_FILE` at function-definition time, so patching
    the constant after import does not change the already-bound default --
    this test would then depend on whether this checkout happens to have a
    real `.aws/kms-test-key.arn` on disk, which is exactly the kind of
    ambient-state dependency a deterministic test must not have.
    """
    import tools.render_aws_policy as render_aws_policy_module
    from tools.render_aws_policy import main

    def _raise_absent(arn_file: Path = tmp_path / "definitely-absent.arn") -> str:
        raise FileNotFoundError(f"{arn_file} is absent")

    monkeypatch.setattr(render_aws_policy_module, "resolve_kms_key_id", _raise_absent)
    out_path = tmp_path / "rendered.json"

    exit_code = main(
        [
            "--account",
            _ACCOUNT,
            "--bucket-prefix",
            "kf-example",
            "--out",
            str(out_path),
        ]
    )

    assert exit_code == 0
    parsed = json.loads(out_path.read_text())
    sids = [s["Sid"] for s in parsed["Statement"]]
    assert "KMSLayerW" not in sids


def test_main_chmods_a_pre_existing_regular_file_at_out(tmp_path: Path) -> None:
    """A pre-existing *regular* file at `--out` must end up at `0o600`.

    `O_CREAT`'s mode argument to `os.open()` is only applied when the
    call actually creates the file; `O_NOFOLLOW` does not fire here
    either -- a regular file is not a symlink. Both those earlier fixes
    are no-ops for this case, so it needs its own guard. A bug that would
    fail this: dropping the `chmod` call when `write_text()` +
    `chmod(0o600)` was replaced by a single `os.open()` -- the account id
    and KMS key id then land in a file that keeps whatever mode it already
    had (world-writable-and-readable `0o666` here), in the same
    predictable `/tmp` location the module's own usage example documents.
    """
    from tools.render_aws_policy import main

    out_path = tmp_path / "predictable-name.json"
    out_path.write_text("pre-existing content\n")
    out_path.chmod(0o666)

    main(
        [
            "--account",
            _ACCOUNT,
            "--kms-key-id",
            _KEY_ID,
            "--bucket-prefix",
            "kf-example",
            "--out",
            str(out_path),
        ]
    )
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
