"""S3-compatible storage for the contents of task attachments, configured from environment variables.

Set TASKBOARD_S3_BUCKET to enable it. TASKBOARD_S3_ENDPOINT_URL points at a non-AWS service such as
the rustfs service in docker-compose.yml; leave it unset for AWS S3. Without TASKBOARD_S3_ACCESS_KEY
and TASKBOARD_S3_SECRET_KEY, boto3 falls back to its usual credential sources. The bucket is created
if it doesn't exist yet.
"""

import os

import boto3
import streamlit as st
from botocore.config import Config
from botocore.exceptions import ClientError

S3_ENV = {
    "endpoint_url": "TASKBOARD_S3_ENDPOINT_URL",
    "bucket": "TASKBOARD_S3_BUCKET",
    "access_key": "TASKBOARD_S3_ACCESS_KEY",
    "secret_key": "TASKBOARD_S3_SECRET_KEY",
    "region": "TASKBOARD_S3_REGION",
}
DEFAULT_REGION = "us-east-1"
STATUS_TTL = 30  # seconds between connectivity checks
CACHED_FILES = 16  # file contents kept in memory for previews and downloads


def s3_settings():
    """The S3 settings from the environment, or None unless a bucket is configured."""
    settings = {key: os.environ.get(env, "").strip() or None for key, env in S3_ENV.items()}
    if not settings["bucket"]:
        return None
    settings["region"] = settings["region"] or DEFAULT_REGION
    return settings


def s3_configured():
    return s3_settings() is not None


@st.cache_resource(show_spinner=False)
def _client(endpoint_url, access_key, secret_key, region):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        # Path-style URLs (endpoint/bucket/key) work with self-hosted services without wildcard DNS.
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )


def client():
    settings = s3_settings()
    return _client(
        settings["endpoint_url"], settings["access_key"], settings["secret_key"], settings["region"]
    )


def bucket():
    return s3_settings()["bucket"]


def account_prefix(user_id):
    # Every object of an account shares this prefix, so deleting the account can sweep them all.
    return f"users/{user_id}/"


def object_key(user_id, task_id, file_id):
    return f"{account_prefix(user_id)}tasks/{task_id}/{file_id}"


def _ensure_bucket():
    try:
        client().head_bucket(Bucket=bucket())
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        region = s3_settings()["region"]
        # us-east-1 is the one region that rejects an explicit location constraint.
        location = {} if region == DEFAULT_REGION else {
            "CreateBucketConfiguration": {"LocationConstraint": region}
        }
        client().create_bucket(Bucket=bucket(), **location)


@st.cache_data(ttl=STATUS_TTL, show_spinner=False)
def connection_error():
    """Why the bucket can't be used, or None if it can. Cached so reruns don't ping it."""
    try:
        _ensure_bucket()
    except Exception as e:  # noqa: BLE001 - any failure here means "not reachable"
        return f"{type(e).__name__}: {e}".splitlines()[0]
    return None


def put_file(key, upload, content_type):
    """Store an uploaded file's contents under `key`, replacing whatever was there."""
    upload.seek(0)
    # upload_fileobj switches to a multipart upload for large files.
    client().upload_fileobj(upload, bucket(), key, ExtraArgs={"ContentType": content_type})


# `version` isn't used in the body: it's part of the cache key, so replaced contents are fetched again.
@st.cache_data(max_entries=CACHED_FILES, ttl=3600, show_spinner=False)
def read_file(key, version):
    return client().get_object(Bucket=bucket(), Key=key)["Body"].read()


def delete_file(key):
    client().delete_object(Bucket=bucket(), Key=key)


def delete_prefix(prefix):
    """Delete every object whose key starts with `prefix`."""
    # Each page holds at most 1000 keys, the most one delete_objects call accepts.
    for page in client().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        if objects := [{"Key": obj["Key"]} for obj in page.get("Contents", [])]:
            result = client().delete_objects(
                Bucket=bucket(), Delete={"Objects": objects, "Quiet": True}
            )
            if errors := result.get("Errors"):
                raise RuntimeError(f"Couldn't delete {errors[0]['Key']}: {errors[0]['Message']}")


def file_storage_sidebar():
    """File storage status for the sidebar; nothing when it isn't configured."""
    if not s3_configured():
        return
    if error := connection_error():
        st.caption(":material/cloud_off: :red[File storage unreachable]", help=error)
    else:
        st.caption(":material/cloud_done: File storage connected")
