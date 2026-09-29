from __future__ import annotations

import io
import threading
from urllib.error import HTTPError, URLError

import pytest

from chart_manager.integrations.schema_sources import (
    SchemaDownload,
    SchemaSourceClient,
    raw_github_url,
)
from chart_manager.services.schemas.errors import (
    SchemaNotFoundError,
    SchemaSourceEnvironmentError,
    SchemaSourceIntegrityError,
)


class _Response:
    def __init__(self, content: bytes, headers: dict[str, str] | None = None) -> None:
        self._content = io.BytesIO(content)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int) -> bytes:
        return self._content.read(size)


def test_ref_resolution_requires_a_full_lowercase_sha() -> None:
    client = SchemaSourceClient(
        opener=lambda *_args, **_kwargs: _Response(b'{"sha":"' + b"a" * 40 + b'"}')
    )

    assert client.resolve_github_ref("owner/repo", "feature/ref") == "a" * 40


def test_ref_resolution_rejects_invalid_github_payload() -> None:
    client = SchemaSourceClient(opener=lambda *_args, **_kwargs: _Response(b'{"sha":"short"}'))

    with pytest.raises(SchemaSourceIntegrityError, match="full commit SHA"):
        client.resolve_github_ref("owner/repo", "main")


def test_http_not_found_is_distinct_from_environment_failure() -> None:
    def missing(*_args, **_kwargs):
        raise HTTPError("https://example", 404, "missing", {}, None)

    with pytest.raises(SchemaNotFoundError):
        SchemaSourceClient(opener=missing).download(SchemaDownload("x", "https://example"))

    def offline(*_args, **_kwargs):
        raise URLError("network down")

    with pytest.raises(SchemaSourceEnvironmentError, match="unreachable"):
        SchemaSourceClient(opener=offline).download(SchemaDownload("x", "https://example"))


def test_download_is_size_and_checksum_bounded() -> None:
    client = SchemaSourceClient(
        max_bytes=2,
        opener=lambda *_args, **_kwargs: _Response(b"123"),
    )

    with pytest.raises(SchemaSourceIntegrityError, match="exceeds"):
        client.download(SchemaDownload("x", "https://example"))

    checksum_client = SchemaSourceClient(
        opener=lambda *_args, **_kwargs: _Response(b"{}"),
    )
    with pytest.raises(SchemaSourceIntegrityError, match="checksum mismatch"):
        checksum_client.download(
            SchemaDownload("x", "https://example", expected_sha256="sha256:" + "0" * 64)
        )


def test_download_many_deduplicates_urls() -> None:
    calls = 0
    guard = threading.Lock()

    def opener(*_args, **_kwargs):
        nonlocal calls
        with guard:
            calls += 1
        return _Response(b"{}")

    batch = SchemaSourceClient(opener=opener).download_many(
        [
            SchemaDownload("a", "https://example/schema"),
            SchemaDownload("b", "https://example/schema"),
        ]
    )

    assert calls == 1
    assert batch.content == {"a": b"{}", "b": b"{}"}


def test_raw_url_rejects_parent_traversal() -> None:
    with pytest.raises(ValueError):
        raw_github_url("owner/repo", "a" * 40, "../secret")
