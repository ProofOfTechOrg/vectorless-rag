from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from types_boto3_s3 import S3Client

from vectorless_rag.config import Settings


class ObjectStore(Protocol):
    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...


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
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

    async def put_file(self, key: str, path: Path, content_type: str) -> None:
        await asyncio.to_thread(
            self.client.upload_file,
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "ServerSideEncryption": "AES256"},
        )

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


class MemoryObjectStore:
    """Deterministic object store for tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def exists(self, key: str) -> bool:
        return key in self.objects
