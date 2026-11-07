from __future__ import annotations

import logging
import mimetypes
import threading
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from mindin.backends import FETCH_WORKERS, MUST_NOT_EXIST, Entry, NotFound, VersionConflict, is_vault_key

MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
CONFLICT_CODES = frozenset({"PreconditionFailed", "412", "ConditionalRequestConflict", "409"})
UNSUPPORTED_CODES = frozenset({"NotImplemented", "501"})
log = logging.getLogger(__name__)
MARKDOWN = "text/markdown; charset=utf-8"
PROBE_FOLDER = ".mindin-probe/"
_delete_probes: dict[tuple[str, str], bool] = {}
_probe_lock = threading.Lock()


def _etag(raw: str | None) -> str:
    return (raw or "").strip('"')


class S3Backend:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str = "",
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self.conditional_puts = True
        self.conditional_deletes = True
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4", max_pool_connections=FETCH_WORKERS),
        )

    def _object_key(self, key: str) -> str:
        return self.prefix + key

    def _vault_key(self, object_key: str) -> str:
        return object_key[len(self.prefix) :] if self.prefix else object_key

    def list(self) -> list[Entry]:
        entries: list[Entry] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = self._vault_key(obj["Key"])
                if not is_vault_key(key):
                    continue
                entries.append(
                    Entry(
                        key=key,
                        version=_etag(obj.get("ETag")),
                        mtime=obj["LastModified"],
                        size=obj.get("Size", 0),
                    )
                )
        return sorted(entries, key=lambda entry: entry.key)

    def list_all(self, prefix: str = "") -> list[Entry]:
        entries: list[Entry] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            for obj in page.get("Contents", []):
                key = self._vault_key(obj["Key"])
                if not key or key.startswith(PROBE_FOLDER):
                    continue
                entries.append(
                    Entry(key=key, version=_etag(obj.get("ETag")), mtime=obj["LastModified"], size=obj.get("Size", 0))
                )
        return sorted(entries, key=lambda entry: entry.key)

    def get(self, key: str) -> tuple[str, str]:
        data, version = self.get_bytes(key)
        return data.decode("utf-8"), version

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in MISSING_CODES:
                raise NotFound(key) from exc
            raise
        return response["Body"].read(), _etag(response.get("ETag"))

    def current_version(self, key: str) -> str | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._object_key(key))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in MISSING_CODES:
                return None
            raise
        return _etag(response.get("ETag"))

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        return self.put_checked(key, text.encode("utf-8"), expected_version, MARKDOWN)

    def put_checked(
        self, key: str, data: bytes, expected_version: str | None = None, content_type: str = MARKDOWN
    ) -> str:
        if expected_version is None:
            return self.put_bytes(key, data, content_type)
        if self.conditional_puts:
            condition = (
                {"IfNoneMatch": "*"} if expected_version == MUST_NOT_EXIST else {"IfMatch": f'"{expected_version}"'}
            )
            try:
                return self.put_bytes(key, data, content_type, condition)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in CONFLICT_CODES or (code in MISSING_CODES and expected_version != MUST_NOT_EXIST):
                    raise VersionConflict(f"{key} changed since it was read (expected {expected_version})") from exc
                if code not in UNSUPPORTED_CODES:
                    raise
                self.conditional_puts = False
                log.warning("mindin: this S3 server refuses conditional PUTs; checking the etag before each write")
        current = self.current_version(key)
        if (current is not None) if expected_version == MUST_NOT_EXIST else (current != expected_version):
            raise VersionConflict(f"{key} changed since it was read (etag {current}, expected {expected_version})")
        return self.put_bytes(key, data, content_type)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None, condition: dict | None = None) -> str:
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=self._object_key(key),
            Body=data,
            ContentType=content_type or mimetypes.guess_type(key)[0] or "application/octet-stream",
            **(condition or {}),
        )
        return _etag(response.get("ETag"))

    def mkdir(self, key: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._object_key(key.rstrip("/") + "/"), Body=b"")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key))

    def delete_checked(self, key: str, expected_version: str) -> None:
        if self.conditional_deletes and self._honours_conditional_deletes():
            try:
                key_name = self._object_key(key)
                self.client.delete_object(Bucket=self.bucket, Key=key_name, IfMatch=f'"{expected_version}"')
                return
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in CONFLICT_CODES or code in MISSING_CODES:
                    raise VersionConflict(f"{key} changed since it was read (expected {expected_version})") from exc
                if code not in UNSUPPORTED_CODES:
                    raise
                self.conditional_deletes = False
                log.warning("mindin: this S3 server refuses conditional deletes; checking the etag first")
        if self.current_version(key) != expected_version:
            raise VersionConflict(f"{key} changed since it was read (expected {expected_version})")
        self.delete(key)

    def _honours_conditional_deletes(self) -> bool:
        server = (self.client.meta.endpoint_url, self.bucket)
        with _probe_lock:
            if server not in _delete_probes:
                _delete_probes[server] = self._probe_conditional_delete()
        self.conditional_deletes = _delete_probes[server]
        return self.conditional_deletes

    def _probe_conditional_delete(self) -> bool:
        probe = self._object_key(f"{PROBE_FOLDER}delete-{uuid.uuid4().hex}")
        try:
            self.client.put_object(Bucket=self.bucket, Key=probe, Body=b"")
            try:
                self.client.delete_object(Bucket=self.bucket, Key=probe, IfMatch='"mindin-probe-mismatch"')
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in UNSUPPORTED_CODES:
                    log.warning("mindin: this S3 server refuses conditional deletes; checking the etag first")
                    return False
                if code not in CONFLICT_CODES:
                    raise
                if self._probe_exists(probe):
                    return True
            log.warning("mindin: this S3 server ignores If-Match on deletes; checking the etag first")
            return False
        except Exception as exc:
            log.warning("mindin: could not probe conditional deletes on this S3 server (%s); checking the etag", exc)
            return False
        finally:
            try:
                self.client.delete_object(Bucket=self.bucket, Key=probe)
            except Exception as exc:
                log.warning("mindin: could not remove the delete probe %s: %s", probe, exc)

    def _probe_exists(self, probe: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=probe)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in MISSING_CODES:
                return False
            raise
        return True

    def move(self, src: str, dst: str) -> None:
        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=self._object_key(dst),
                CopySource={"Bucket": self.bucket, "Key": self._object_key(src)},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in MISSING_CODES:
                raise NotFound(src) from exc
            raise
        self.delete(src)
