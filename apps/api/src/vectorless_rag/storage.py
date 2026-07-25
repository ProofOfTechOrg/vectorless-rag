from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client

from vectorless_rag.config import Settings


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    last_modified: datetime | None


class ObjectStore(Protocol):
    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...
    async def put_bytes_if_absent(self, key: str, data: bytes, content_type: str) -> bool: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def list_objects(self, prefix: str) -> list[ObjectInfo]: ...
    async def delete_objects(self, keys: list[str]) -> None: ...


class S3ObjectStore:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.object_store_bucket
        self.client: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=settings.object_store_endpoint,
            aws_access_key_id=settings.object_store_access_key.get_secret_value(),
            aws_secret_access_key=settings.object_store_secret_key.get_secret_value(),
            region_name=settings.object_store_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def ensure_bucket(self) -> None:
        def create() -> None:
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError:
                self.client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(create)

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        def put() -> None:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                    ServerSideEncryption="AES256",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "NotImplemented":
                    raise
                self.client.put_object(
                    Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
                )

        await asyncio.to_thread(put)

    async def put_file(self, key: str, path: Path, content_type: str) -> None:
        def upload() -> None:
            arguments = {"ContentType": content_type, "ServerSideEncryption": "AES256"}
            try:
                self.client.upload_file(str(path), self.bucket, key, ExtraArgs=arguments)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "NotImplemented":
                    raise
                self.client.upload_file(
                    str(path), self.bucket, key, ExtraArgs={"ContentType": content_type}
                )

        await asyncio.to_thread(upload)

    async def put_bytes_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        def put() -> bool:
            arguments: dict[str, object] = {
                "Bucket": self.bucket,
                "Key": key,
                "Body": data,
                "ContentType": content_type,
                "IfNoneMatch": "*",
                "ServerSideEncryption": "AES256",
            }
            try:
                self.client.put_object(**arguments)  # pyright: ignore[reportArgumentType]
                return True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in {"PreconditionFailed", "412"}:
                    return False
                if code != "NotImplemented":
                    raise
                arguments.pop("ServerSideEncryption")
                try:
                    self.client.put_object(**arguments)  # pyright: ignore[reportArgumentType]
                    return True
                except ClientError as fallback:
                    if fallback.response.get("Error", {}).get("Code") in {
                        "PreconditionFailed",
                        "412",
                    }:
                        return False
                    raise

        return await asyncio.to_thread(put)

    async def get_bytes(self, key: str) -> bytes:
        response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    async def list_objects(self, prefix: str) -> list[ObjectInfo]:
        def list_all() -> list[ObjectInfo]:
            result: list[ObjectInfo] = []
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for value in page.get("Contents", []):
                    key = value.get("Key")
                    size = value.get("Size")
                    if key is None or size is None:
                        continue
                    result.append(
                        ObjectInfo(
                            key=key,
                            size=size,
                            last_modified=value.get("LastModified"),
                        )
                    )
            return result

        return await asyncio.to_thread(list_all)

    async def delete_objects(self, keys: list[str]) -> None:
        if not keys:
            return

        def delete_all() -> None:
            for start in range(0, len(keys), 1_000):
                chunk = keys[start : start + 1_000]
                response = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
                )
                errors = response.get("Errors", [])
                if errors:
                    raise RuntimeError("object-store deletion was incomplete")

        await asyncio.to_thread(delete_all)


class MemoryObjectStore:
    """Deterministic object store for tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, datetime | None] = {}

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = data
        self.modified[key] = None

    async def put_bytes_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        del content_type
        if key in self.objects:
            return False
        self.objects[key] = data
        self.modified[key] = None
        return True

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def list_objects(self, prefix: str) -> list[ObjectInfo]:
        return [
            ObjectInfo(key=key, size=len(value), last_modified=self.modified.get(key))
            for key, value in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    async def delete_objects(self, keys: list[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)
            self.modified.pop(key, None)
