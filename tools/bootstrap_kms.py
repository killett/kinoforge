"""Idempotent KMS bootstrap for Layer W (S3 + GCS real-cloud verification).

Creates one AWS KMS key + one GCP Cloud KMS keyring/key. Idempotent: re-runs
detect the persisted ARN / resource-name file plus an existing key and skip.

Usage::

    pixi run cloud:bootstrap-kms

Rotation policy: BOTH keys are NOT auto-rotated. Rotation would invalidate
Layer W fixtures (the key id is embedded in every recorded encryption response).

Persisted files (gitignored):
    .aws/kms-test-key.arn      — AWS KMS key ARN
    .gcp/kms-test-key.name     — GCP Cloud KMS crypto-key resource name
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

    On re-run, if ``.aws/kms-test-key.arn`` exists and ``kms:DescribeKey``
    returns successfully, logs ``skipped — key already exists`` and returns.
    """
    kms = boto3.client("kms", region_name=AWS_REGION)

    if AWS_KEY_FILE.exists():
        existing_arn = AWS_KEY_FILE.read_text().strip()
        try:
            kms.describe_key(KeyId=existing_arn)
            logger.info("AWS: skipped — key already exists at %s", existing_arn)
            return
        except ClientError as exc:
            logger.warning("AWS: persisted ARN unusable (%s); creating fresh key", exc)

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

    created = kms.create_key(
        Description="kinoforge realcloud tests CMK (Layer W)",
        KeyUsage="ENCRYPT_DECRYPT",
        Policy=json.dumps(policy),
        Tags=[
            {"TagKey": "Project", "TagValue": "kinoforge"},
            {"TagKey": "Layer", "TagValue": "W"},
            {"TagKey": "ManagedBy", "TagValue": "bootstrap_kms.py"},
        ],
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


def bootstrap_gcp() -> None:
    """Create GCP Cloud KMS keyring + key + IAM bindings. Idempotent.

    Binds BOTH:
    - ``kinoforge-runner`` service account (SA used by tests at runtime)
    - GCS service agent (``service-<project_number>@gs-project-accounts.iam.gserviceaccount.com``)
      — required so GCS can wrap/unwrap the CMEK key when writing/reading objects.
    """
    kms_client = kms_v1.KeyManagementServiceClient()

    # Derive project id from the service-account key (avoids a separate env var).
    sa_email = _read_sa_email()
    project_id = sa_email.split("@")[1].split(".iam.")[0]  # "<GCP_PROJECT>"

    location_path = f"projects/{project_id}/locations/{GCP_LOCATION}"
    keyring_path = f"{location_path}/keyRings/{GCP_KEYRING}"
    key_path = f"{keyring_path}/cryptoKeys/{GCP_KEY}"

    if GCP_KEY_FILE.exists():
        try:
            kms_client.get_crypto_key(name=key_path)
            logger.info("GCS: skipped — key already exists at %s", key_path)
            return
        except NotFound:
            logger.warning(
                "GCS: persisted key name not found in API; creating fresh key"
            )

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

    if target_binding is None:
        # google.iam.v1 Binding — must be added via policy.bindings.add()
        target_binding = policy.bindings.add()
        target_binding.role = GCP_ROLE

    for member in members_to_bind:
        if member not in target_binding.members:
            target_binding.members.append(member)
            logger.info("GCS: adding IAM member %s → %s", member, GCP_ROLE)

    kms_client.set_iam_policy(request={"resource": key_path, "policy": policy})
    logger.info("GCS: IAM bindings set for %d member(s)", len(members_to_bind))

    # Persist the key resource name.
    GCP_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    GCP_KEY_FILE.write_text(key_path)
    logger.info("GCS: key resource name persisted to %s", GCP_KEY_FILE)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run AWS + GCP KMS bootstrap. Returns 0 on success."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logger.info("=== KMS bootstrap start ===")
    bootstrap_aws()
    bootstrap_gcp()
    logger.info("=== KMS bootstrap complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
