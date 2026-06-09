"""Idempotent KMS bootstrap for Layer W (S3 + GCS real-cloud verification).

Creates one AWS KMS key + one GCP Cloud KMS keyring/key. Idempotent: re-runs
detect the persisted ARN / resource-name file plus an existing key and skip.

Usage::

    pixi run cloud:bootstrap-kms

Rotation policy: BOTH keys are NOT auto-rotated. Rotation would invalidate
Layer W fixtures (the key id is embedded in every recorded encryption response).

Persisted files (gitignored):
    .aws/kms-test-key.arn      — AWS KMS key ARN
    .gcp/kms-test-key.name     — GCP Cloud KMS primary crypto-key version resource name
                                 (e.g. projects/.../cryptoKeys/.../cryptoKeyVersions/1)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import kms_v1, resourcemanager_v3

logger = logging.getLogger("bootstrap_kms")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

AWS_REGION = "us-east-1"
AWS_ALIAS = "alias/kinoforge-realcloud-tests"
# Path is relative to the repo root (script must be run via pixi run from root).
AWS_KEY_FILE = Path(".aws/kms-test-key.arn")
AWS_IAM_USER = "kinoforge-ci"

GCP_LOCATION = "us-central1"
GCP_KEYRING = "kinoforge-realcloud-tests"
GCP_KEY = "bucket-cmek"
GCP_KEY_FILE = Path(".gcp/kms-test-key.name")
GCP_ROLE = "roles/cloudkms.cryptoKeyEncrypterDecrypter"


# ---------------------------------------------------------------------------
# AWS KMS
# ---------------------------------------------------------------------------


def bootstrap_aws() -> None:
    """Create AWS KMS key + alias + resource policy. Idempotent.

    Idempotence checks (in order):
    1. ``.aws/kms-test-key.arn`` exists and ``kms:DescribeKey`` succeeds → skip.
    2. ``kms:DescribeKey`` on ``alias/kinoforge-realcloud-tests`` succeeds →
       record the ARN and skip (key already exists but ARN file is missing).
    3. ``KINOFORGE_AWS_KMS_ARN`` env var set → record it and skip (operator
       pre-provisioned the key via admin credentials or the AWS Console; this
       path is used when ``kinoforge-ci`` lacks ``kms:CreateKey``).
    4. Otherwise attempt ``kms:CreateKey`` (requires admin-level IAM policy).

    The ``kinoforge-ci`` IAM user only holds ``AmazonS3FullAccess`` and therefore
    cannot call ``kms:CreateKey``.  If you hit ``AccessDeniedException`` here,
    either:
      a. In the AWS Console, temporarily attach the managed policy
         ``AWSKeyManagementServicePowerUser`` (or a custom policy with
         ``kms:CreateKey`` + ``kms:CreateAlias`` + ``kms:PutKeyPolicy``) to the
         ``kinoforge-ci`` user, run this script, then detach it; or
      b. Create the key via the AWS Console / an admin CLI profile, copy the ARN,
         then re-run with ``KINOFORGE_AWS_KMS_ARN=<arn> pixi run cloud:bootstrap-kms``.
    """
    kms = boto3.client("kms", region_name=AWS_REGION)

    # --- Check 1: persisted ARN file ---
    if AWS_KEY_FILE.exists():
        existing_arn = AWS_KEY_FILE.read_text().strip()
        try:
            resp = kms.describe_key(KeyId=existing_arn)
            key_state = resp["KeyMetadata"]["KeyState"]
            if key_state != "Enabled":
                logger.warning(
                    "AWS: persisted ARN %s exists but KeyState=%s (not Enabled); "
                    "persisted file is stale — recreating key",
                    existing_arn,
                    key_state,
                )
                AWS_KEY_FILE.unlink()
                # Fall through to alias check / creation path.
            else:
                logger.info("AWS: skipped — key already exists at %s", existing_arn)
                return
        except ClientError as exc:
            logger.warning("AWS: persisted ARN unusable (%s); continuing", exc)

    # --- Check 2: alias already registered in KMS ---
    try:
        response = kms.describe_key(KeyId=AWS_ALIAS)
        arn = response["KeyMetadata"]["Arn"]
        key_state = response["KeyMetadata"]["KeyState"]
        if key_state != "Enabled":
            logger.error(
                "AWS: alias %s resolves to key %s but KeyState=%s — "
                "key is not usable; manual remediation required",
                AWS_ALIAS,
                arn,
                key_state,
            )
            raise RuntimeError(
                f"AWS KMS alias {AWS_ALIAS!r} points to a key in state "
                f"{key_state!r}; cancel pending deletion or create a new key"
            )
        AWS_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        AWS_KEY_FILE.write_text(arn)
        logger.info(
            "AWS: alias %s already exists (ARN %s); persisted to %s",
            AWS_ALIAS,
            arn,
            AWS_KEY_FILE,
        )
        return
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NotFoundException":
            pass  # Alias doesn't exist yet; fall through to creation.
        else:
            raise

    # --- Check 3: operator-supplied ARN via env var ---
    env_arn = os.environ.get("KINOFORGE_AWS_KMS_ARN", "").strip()
    if env_arn:
        try:
            kms.describe_key(KeyId=env_arn)
            AWS_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            AWS_KEY_FILE.write_text(env_arn)
            logger.info(
                "AWS: KINOFORGE_AWS_KMS_ARN provided; key verified and persisted to %s",
                AWS_KEY_FILE,
            )
            return
        except ClientError as exc:
            raise RuntimeError(
                f"KINOFORGE_AWS_KMS_ARN={env_arn!r} was provided but "
                f"kms:DescribeKey failed: {exc}"
            ) from exc

    # --- Check 4: attempt to create the key (requires kms:CreateKey) ---
    # Look up the AWS account number for the key policy principals.
    sts = boto3.client("sts")
    account = sts.get_caller_identity()["Account"]

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EnableRoot",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:root"},
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": "AllowKinoforgeCI",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:user/{AWS_IAM_USER}"},
                "Action": [
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:DescribeKey",
                ],
                "Resource": "*",
            },
        ],
    }

    # Tags are omitted — kinoforge-ci lacks kms:TagResource; tagging can be
    # done from the AWS Console by a privileged user if needed.
    created = kms.create_key(
        Description="kinoforge realcloud tests CMK",
        KeyUsage="ENCRYPT_DECRYPT",
        Policy=json.dumps(policy),
    )
    arn = created["KeyMetadata"]["Arn"]

    # Create the alias so tests can reference it by a stable name.
    kms.create_alias(AliasName=AWS_ALIAS, TargetKeyId=arn)

    AWS_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    AWS_KEY_FILE.write_text(arn)
    logger.info("AWS: created key %s + alias %s", arn, AWS_ALIAS)


# ---------------------------------------------------------------------------
# GCP Cloud KMS
# ---------------------------------------------------------------------------


def _read_sa_email() -> str:
    """Extract client_email from the service-account JSON credential file."""
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not creds_path:
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS is not set; "
            "ensure pixi.toml [activation.env] is loaded."
        )
    sa_data: dict[str, str] = json.loads(Path(creds_path).read_text())
    return sa_data["client_email"]


def _resolve_project_number(project_id: str) -> str:
    """Resolve a GCP project *number* from its project *id*.

    Uses the Cloud Resource Manager v3 API. If the API call fails, raises so
    the caller can surface the error rather than silently using a wrong value.
    """
    rm = resourcemanager_v3.ProjectsClient()
    project = rm.get_project(name=f"projects/{project_id}")
    # project.name is "projects/<number>"
    return project.name.split("/")[-1]


def _ensure_gcs_service_agent(project_id: str) -> None:
    """Ensure the GCS service agent exists by calling generateServiceIdentity.

    The service agent (``service-<project_number>@gs-project-accounts.iam.gserviceaccount.com``)
    is created lazily by GCP and may not exist until it is explicitly requested.
    This call is idempotent — if the agent already exists, the API returns 200 OK.
    """
    import google.auth.transport.requests as _tr
    import requests as _req
    from google.oauth2 import service_account as _sa

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    creds: _sa.Credentials = _sa.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        creds_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds.refresh(_tr.Request())  # type: ignore[no-untyped-call]

    url = (
        f"https://serviceusage.googleapis.com/v1beta1/projects/{project_id}"
        f"/services/storage.googleapis.com:generateServiceIdentity"
    )
    resp = _req.post(
        url,
        headers={"Authorization": f"Bearer {creds.token}"},
        json={},
        timeout=30,
    )
    if resp.ok:
        logger.info("GCS: generateServiceIdentity OK (status %d)", resp.status_code)
    else:
        # 403 means the SA lacks serviceusage.services.use — warn but proceed.
        logger.warning(
            "GCS: generateServiceIdentity returned %d — agent may not exist yet; "
            "proceeding with IAM binding (will fail if agent is absent)",
            resp.status_code,
        )


def bootstrap_gcp() -> None:
    """Create GCP Cloud KMS keyring + key + IAM bindings. Idempotent.

    Binds BOTH:
    - ``kinoforge-runner`` service account (SA used by tests at runtime)
    - GCS service agent (``service-<project_number>@gs-project-accounts.iam.gserviceaccount.com``)
      — required so GCS can wrap/unwrap the CMEK key when writing/reading objects.

    The idempotence check uses ``GCP_KEY_FILE`` existence **and** ``get_crypto_key``
    success. If the key exists but the file is absent (e.g. the previous run
    crashed after key creation but before the file write), the function skips key
    and keyring creation but still ensures the IAM bindings and file are set.
    """
    kms_client = kms_v1.KeyManagementServiceClient()

    # Derive project id from the service-account key (avoids a separate env var).
    sa_email = _read_sa_email()
    project_id = sa_email.split("@")[1].split(".iam.")[
        0
    ]  # e.g. "<GCP_PROJECT>"

    location_path = f"projects/{project_id}/locations/{GCP_LOCATION}"
    keyring_path = f"{location_path}/keyRings/{GCP_KEYRING}"
    key_path = f"{keyring_path}/cryptoKeys/{GCP_KEY}"

    # Full idempotence: file exists AND key is reachable — skip creation but
    # still fall through to IAM verification and re-persist with versioned name.
    key_already_exists = False
    if GCP_KEY_FILE.exists():
        try:
            kms_client.get_crypto_key(name=key_path)
            key_already_exists = True
            logger.info(
                "GCS: key already exists at %s — skipping creation, "
                "re-verifying IAM bindings",
                key_path,
            )
        except NotFound:
            logger.warning(
                "GCS: persisted key name not found in API; creating fresh key"
            )

    if not key_already_exists:
        # Partial idempotence: key exists in KMS but file wasn't written (prior crash).
        try:
            kms_client.get_crypto_key(name=key_path)
            key_already_exists = True
            logger.info(
                "GCS: crypto key already exists in KMS (prior partial run); "
                "skipping key/keyring creation, proceeding to IAM + file write"
            )
        except NotFound:
            pass

    if not key_already_exists:
        # --- Create keyring (idempotent) ---
        try:
            kms_client.create_key_ring(
                parent=location_path,
                key_ring_id=GCP_KEYRING,
                key_ring=kms_v1.KeyRing(),
            )
            logger.info("GCS: created keyring %s", keyring_path)
        except AlreadyExists:
            logger.info("GCS: keyring %s already exists, continuing", keyring_path)

        # --- Create crypto key (idempotent) ---
        try:
            kms_client.create_crypto_key(
                parent=keyring_path,
                crypto_key_id=GCP_KEY,
                crypto_key=kms_v1.CryptoKey(
                    purpose=kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT,
                    version_template=kms_v1.CryptoKeyVersionTemplate(
                        algorithm=kms_v1.CryptoKeyVersion.CryptoKeyVersionAlgorithm.GOOGLE_SYMMETRIC_ENCRYPTION
                    ),
                ),
            )
            logger.info("GCS: created crypto key %s", key_path)
        except AlreadyExists:
            logger.info("GCS: crypto key %s already exists, continuing", key_path)

    # --- Ensure GCS service agent exists before binding it ---
    _ensure_gcs_service_agent(project_id)

    # --- IAM bindings ---
    # 1. kinoforge-runner SA (used by tests)
    # 2. GCS service agent (needed for CMEK on bucket writes)
    project_number = _resolve_project_number(project_id)
    gcs_agent = f"service-{project_number}@gs-project-accounts.iam.gserviceaccount.com"

    members_to_bind = [
        f"serviceAccount:{sa_email}",
        f"serviceAccount:{gcs_agent}",
    ]

    # Fetch current policy and add any missing bindings.
    policy = kms_client.get_iam_policy(request={"resource": key_path})

    # Find or create the target role binding.
    target_binding = None
    for binding in policy.bindings:
        if binding.role == GCP_ROLE:
            target_binding = binding
            break

    needs_update = False
    if target_binding is None:
        # google.iam.v1 Binding — must be added via policy.bindings.add()
        target_binding = policy.bindings.add()
        target_binding.role = GCP_ROLE
        needs_update = True

    for member in members_to_bind:
        if member not in target_binding.members:
            target_binding.members.append(member)
            logger.info("GCS: adding IAM member %s → %s", member, GCP_ROLE)
            needs_update = True

    if needs_update:
        kms_client.set_iam_policy(request={"resource": key_path, "policy": policy})
        logger.info("GCS: IAM policy updated")
    else:
        logger.info("GCS: IAM bindings already correct")

    # Persist the primary-version resource name (e.g. .../cryptoKeyVersions/1).
    # This is what callers need to reference the active key version (spec §6.2.5).
    try:
        primary_version_name = kms_client.get_crypto_key(name=key_path).primary.name
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "GCS: could not retrieve primary version name (%s); "
            "falling back to key path",
            exc,
        )
        primary_version_name = key_path

    GCP_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    GCP_KEY_FILE.write_text(primary_version_name)
    logger.info(
        "GCS: primary-version resource name persisted to %s (%s)",
        GCP_KEY_FILE,
        primary_version_name,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run AWS + GCP KMS bootstrap. Returns 0 only when BOTH clouds succeed.

    Each cloud is bootstrapped independently so a failure in one does not
    abort the other. Both results are reported at the end.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logger.info("=== KMS bootstrap start ===")

    aws_ok = True
    gcp_ok = True

    try:
        bootstrap_aws()
    except Exception as exc:
        logger.error("AWS bootstrap FAILED: %s", exc)
        aws_ok = False

    try:
        bootstrap_gcp()
    except Exception as exc:
        logger.error("GCP bootstrap FAILED: %s", exc)
        gcp_ok = False

    if aws_ok and gcp_ok:
        logger.info("=== KMS bootstrap complete — both clouds OK ===")
        return 0

    if not aws_ok:
        logger.error(
            "AWS: AccessDeniedException on kms:CreateKey — kinoforge-ci needs a "
            "temporary KMS creation policy. Options:\n"
            "  a) AWS Console → IAM → kinoforge-ci → Add inline policy granting "
            "kms:CreateKey + kms:CreateAlias + kms:PutKeyPolicy, run this script, "
            "then remove the inline policy.\n"
            "  b) Create the key in the AWS Console (alias: alias/kinoforge-realcloud-tests, "
            "region: us-east-1) and re-run with:\n"
            "     KINOFORGE_AWS_KMS_ARN=<arn> pixi run cloud:bootstrap-kms"
        )
    logger.error("=== KMS bootstrap INCOMPLETE — see errors above ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
