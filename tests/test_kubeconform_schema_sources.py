from __future__ import annotations

import io
import threading
from urllib.error import HTTPError, URLError

import pytest

from chart_manager.integrations.kubeconform.github_schema_source import (
    GitHubKubeconformSchemaNotFoundError,
    GitHubKubeconformSchemaSource,
    GitHubKubeconformSchemaSourceEnvironmentError,
    GitHubKubeconformSchemaSourceIntegrityError,
    KubeconformSchemaArtifactRequest,
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
    client = GitHubKubeconformSchemaSource(
        opener=lambda *_args, **_kwargs: _Response(b'{"sha":"' + b"a" * 40 + b'"}'),
    )

    assert client.resolve_ref("owner/repo", "feature/ref") == "a" * 40


def test_ref_resolution_rejects_invalid_github_payload() -> None:
    client = GitHubKubeconformSchemaSource(
        opener=lambda *_args, **_kwargs: _Response(b'{"sha":"short"}'),
    )

    with pytest.raises(
        GitHubKubeconformSchemaSourceIntegrityError,
        match="full commit SHA",
    ):
        client.resolve_ref("owner/repo", "main")


def test_http_not_found_is_distinct_from_environment_failure() -> None:
    def missing(*_args, **_kwargs):
        raise HTTPError("https://example", 404, "missing", {}, None)

    with pytest.raises(GitHubKubeconformSchemaNotFoundError):
        GitHubKubeconformSchemaSource(opener=missing).fetch(
            KubeconformSchemaArtifactRequest("x", "https://example")
        )

    def offline(*_args, **_kwargs):
        raise URLError("network down")

    with pytest.raises(
        GitHubKubeconformSchemaSourceEnvironmentError,
        match="unreachable",
    ):
        GitHubKubeconformSchemaSource(opener=offline).fetch(
            KubeconformSchemaArtifactRequest("x", "https://example")
        )


def test_download_is_size_and_checksum_bounded() -> None:
    client = GitHubKubeconformSchemaSource(
        max_bytes=2,
        opener=lambda *_args, **_kwargs: _Response(b"123"),
    )

    with pytest.raises(GitHubKubeconformSchemaSourceIntegrityError, match="exceeds"):
        client.fetch(KubeconformSchemaArtifactRequest("x", "https://example"))

    checksum_client = GitHubKubeconformSchemaSource(
        opener=lambda *_args, **_kwargs: _Response(b"{}"),
    )
    with pytest.raises(
        GitHubKubeconformSchemaSourceIntegrityError,
        match="checksum mismatch",
    ):
        checksum_client.fetch(
            KubeconformSchemaArtifactRequest(
                "x",
                "https://example",
                expected_sha256="sha256:" + "0" * 64,
            )
        )


def test_download_many_deduplicates_urls() -> None:
    calls = 0
    guard = threading.Lock()

    def opener(*_args, **_kwargs):
        nonlocal calls
        with guard:
            calls += 1
        return _Response(b"{}")

    batch = GitHubKubeconformSchemaSource(opener=opener).fetch_many(
        [
            KubeconformSchemaArtifactRequest("a", "https://example/schema"),
            KubeconformSchemaArtifactRequest("b", "https://example/schema"),
        ]
    )

    assert calls == 1
    assert batch.content == {"a": b"{}", "b": b"{}"}


def test_raw_url_rejects_parent_traversal() -> None:
    with pytest.raises(ValueError):
        GitHubKubeconformSchemaSource.artifact_url(
            "owner/repo",
            "a" * 40,
            "../secret",
        )
