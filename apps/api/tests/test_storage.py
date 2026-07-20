from collections.abc import Callable, Generator
from typing import Any, TypeVar, cast
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from vectorless_rag.storage import S3ObjectStore

T = TypeVar("T")


@pytest.fixture(autouse=True)
def run_storage_threads_inline(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    async def run(call: Callable[[], T]) -> T:
        return call()

    monkeypatch.setattr("vectorless_rag.storage.asyncio.to_thread", run)
    yield


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")


async def test_put_bytes_retries_without_encryption_only_when_store_rejects_it() -> None:
    store = cast(Any, object.__new__(S3ObjectStore))
    store.bucket = "documents"
    store.client = MagicMock()
    store.client.put_object.side_effect = [client_error("NotImplemented"), {}]

    await store.put_bytes("fixture.pdf", b"pdf", "application/pdf")

    first, second = store.client.put_object.call_args_list
    assert first.kwargs["ServerSideEncryption"] == "AES256"
    assert "ServerSideEncryption" not in second.kwargs


async def test_put_bytes_preserves_other_object_store_errors() -> None:
    store = cast(Any, object.__new__(S3ObjectStore))
    store.bucket = "documents"
    store.client = MagicMock()
    store.client.put_object.side_effect = client_error("AccessDenied")

    with pytest.raises(ClientError):
        await store.put_bytes("fixture.pdf", b"pdf", "application/pdf")

    store.client.put_object.assert_called_once()
