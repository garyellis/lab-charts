"""GitHub-backed source adapter for immutable kubeconform schemas."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from chart_manager.plumbing.errors import ChartManagerError

_DEFAULT_MAX_BYTES = 16 * 1024 * 1024
_MAX_REF_BYTES = 1024 * 1024

OpenUrl = Callable[..., Any]


class GitHubKubeconformSchemaSourceError(ChartManagerError):
    """Base class for GitHub kubeconform schema source failures."""


class GitHubKubeconformSchemaSourceEnvironmentError(GitHubKubeconformSchemaSourceError):
    """GitHub could not be reached from the caller's environment."""


class GitHubKubeconformSchemaSourceIntegrityError(GitHubKubeconformSchemaSourceError):
    """GitHub returned malformed content or content failed verification."""


class GitHubKubeconformSchemaNotFoundError(GitHubKubeconformSchemaSourceError):
    """GitHub has no object at the requested immutable location."""


@dataclass(frozen=True)
class KubeconformSchemaArtifactRequest:
    """One immutable kubeconform schema artifact to fetch."""

    key: str
    url: str
    expected_sha256: str | None = None


@dataclass(frozen=True)
class KubeconformSchemaArtifactBatch:
    """Fetched artifacts plus keys that GitHub explicitly did not contain."""

    content: dict[str, bytes]
    missing: tuple[str, ...]


class GitHubKubeconformSchemaSource:
    """Resolve GitHub refs and fetch schemas with bounded HTTP requests."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_workers: int = 8,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        opener: OpenUrl = urlopen,
        github_token: str | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("schema source timeout must be positive")
        if not 1 <= max_workers <= 32:
            raise ValueError("schema source max_workers must be between 1 and 32")
        if max_bytes <= 0:
            raise ValueError("schema source max_bytes must be positive")
        self.timeout = timeout
        self.max_workers = max_workers
        self.max_bytes = max_bytes
        self._opener = opener
        self._github_token = github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get(
            "RENOVATE_TOKEN"
        )

    def resolve_ref(self, repository: str, ref: str) -> str:
        url = f"https://api.github.com/repos/{repository}/commits/{quote(ref, safe='')}"
        payload = self._read(url, max_bytes=_MAX_REF_BYTES)
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubKubeconformSchemaSourceIntegrityError(
                f"GitHub returned invalid JSON while resolving {repository}@{ref}: {exc}"
            ) from exc
        sha = document.get("sha") if isinstance(document, dict) else None
        if (
            not isinstance(sha, str)
            or len(sha) != 40
            or any(character not in "0123456789abcdef" for character in sha)
        ):
            raise GitHubKubeconformSchemaSourceIntegrityError(
                f"GitHub response for {repository}@{ref} has no lowercase full commit SHA"
            )
        return sha

    def fetch(self, request: KubeconformSchemaArtifactRequest) -> bytes:
        content = self._read(request.url, max_bytes=self.max_bytes)
        if request.expected_sha256 is not None:
            actual = _content_digest(content)
            if actual != request.expected_sha256:
                raise GitHubKubeconformSchemaSourceIntegrityError(
                    f"schema checksum mismatch for {request.key}: "
                    f"expected {request.expected_sha256}, got {actual}"
                )
        return content

    def fetch_many(
        self,
        requests: list[KubeconformSchemaArtifactRequest]
        | tuple[KubeconformSchemaArtifactRequest, ...],
        *,
        allow_not_found: bool = False,
    ) -> KubeconformSchemaArtifactBatch:
        """Download unique URLs concurrently, preserving request-key results."""
        keyed: dict[str, KubeconformSchemaArtifactRequest] = {}
        urls: dict[str, KubeconformSchemaArtifactRequest] = {}
        keys_by_url: dict[str, list[str]] = {}
        for request in requests:
            previous = keyed.get(request.key)
            if previous is not None and previous != request:
                raise ValueError(f"duplicate schema download key: {request.key}")
            keyed[request.key] = request
            representative = urls.get(request.url)
            if (
                representative is not None
                and representative.expected_sha256 != request.expected_sha256
            ):
                raise ValueError(f"conflicting checksums for schema URL: {request.url}")
            urls[request.url] = request
            keys_by_url.setdefault(request.url, []).append(request.key)
        if not urls:
            return KubeconformSchemaArtifactBatch(content={}, missing=())

        found_by_url: dict[str, bytes] = {}
        missing_urls: set[str] = set()
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(urls)),
            thread_name_prefix="schema-download-",
        ) as pool:
            futures = {pool.submit(self.fetch, request): url for url, request in urls.items()}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    found_by_url[url] = future.result()
                except GitHubKubeconformSchemaNotFoundError:
                    if not allow_not_found:
                        raise
                    missing_urls.add(url)

        content = {
            key: found_by_url[url]
            for url, keys in keys_by_url.items()
            if url in found_by_url
            for key in keys
        }
        missing = tuple(
            sorted(key for url, keys in keys_by_url.items() if url in missing_urls for key in keys)
        )
        return KubeconformSchemaArtifactBatch(content=content, missing=missing)

    def _read(self, url: str, *, max_bytes: int) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json, application/json",
            "User-Agent": "chart-manager-kubeconform-schema-sync",
        }
        if self._github_token and urlsplit(url).hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {self._github_token}"
        request = Request(url, headers=headers)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None:
                    try:
                        declared = int(raw_length)
                    except ValueError as exc:
                        raise GitHubKubeconformSchemaSourceIntegrityError(
                            f"source returned invalid Content-Length for {url}: {raw_length!r}"
                        ) from exc
                    if declared > max_bytes:
                        raise GitHubKubeconformSchemaSourceIntegrityError(
                            f"source response exceeds {max_bytes} bytes for {url}"
                        )
                content = response.read(max_bytes + 1)
        except HTTPError as exc:
            if exc.code == 404:
                raise GitHubKubeconformSchemaNotFoundError(
                    f"schema source has no object at {url}"
                ) from exc
            if exc.code in {403, 429} and urlsplit(url).hostname == "api.github.com":
                raise GitHubKubeconformSchemaSourceEnvironmentError(
                    "GitHub API rate limited schema pin resolution; set a valid "
                    "GITHUB_TOKEN or retry later"
                ) from exc
            raise GitHubKubeconformSchemaSourceEnvironmentError(
                f"schema source request failed with HTTP {exc.code}: {url}"
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise GitHubKubeconformSchemaSourceEnvironmentError(
                f"schema source is unreachable: {url}: {exc}"
            ) from exc
        if len(content) > max_bytes:
            raise GitHubKubeconformSchemaSourceIntegrityError(
                f"source response exceeds {max_bytes} bytes for {url}"
            )
        return content

    @staticmethod
    def artifact_url(repository: str, revision: str, path: str) -> str:
        """Build a raw GitHub URL pinned to an immutable revision."""
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("raw GitHub path must be repository-relative")
        return f"https://raw.githubusercontent.com/{repository}/{revision}/{path}"


def _content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


__all__ = [
    "GitHubKubeconformSchemaNotFoundError",
    "GitHubKubeconformSchemaSource",
    "GitHubKubeconformSchemaSourceEnvironmentError",
    "GitHubKubeconformSchemaSourceError",
    "GitHubKubeconformSchemaSourceIntegrityError",
    "KubeconformSchemaArtifactBatch",
    "KubeconformSchemaArtifactRequest",
]
