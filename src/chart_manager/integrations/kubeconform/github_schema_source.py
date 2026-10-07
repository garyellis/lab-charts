"""GitHub-backed source adapter for immutable kubeconform schemas."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import SecretStr

from chart_manager.plumbing.errors import ChartManagerError

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


def _require_github_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"api.github.com", "raw.githubusercontent.com"}
        or parsed.fragment
    ):
        raise GitHubKubeconformSchemaSourceIntegrityError(
            "schema source URL must use HTTPS on api.github.com or "
            f"raw.githubusercontent.com: {url}"
        )


class _GitHubRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_github_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(newurl).hostname != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


class GitHubKubeconformSchemaSource:
    """Resolve GitHub refs with bounded, authenticated HTTP requests."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        opener: OpenUrl | None = None,
        github_token: SecretStr | None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("schema source timeout must be positive")
        self.timeout = timeout
        self._opener = opener or build_opener(_GitHubRedirectHandler()).open
        # None resolves refs anonymously, at GitHub's lower rate limit.
        # _read only sends credentials to api.github.com, never artifact hosts.
        self._github_token = github_token

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

    def _read(self, url: str, *, max_bytes: int) -> bytes:
        _require_github_url(url)
        headers = {
            "Accept": "application/vnd.github+json, application/json",
            "User-Agent": "chart-manager-kubeconform-schema-sync",
        }
        if self._github_token and urlsplit(url).hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {self._github_token.get_secret_value()}"
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
            if urlsplit(url).hostname == "api.github.com" and (
                exc.code == 429
                or (
                    exc.code == 403
                    and (
                        exc.headers.get("X-RateLimit-Remaining") == "0"
                        or exc.headers.get("Retry-After") is not None
                    )
                )
            ):
                raise GitHubKubeconformSchemaSourceEnvironmentError(
                    _rate_limit_message(
                        token_configured=bool(self._github_token),
                        headers=exc.headers,
                    )
                ) from exc
            if exc.code == 403 and urlsplit(url).hostname == "api.github.com":
                raise GitHubKubeconformSchemaSourceEnvironmentError(
                    "GitHub API returned HTTP 403; check GITHUB_TOKEN permissions and "
                    f"organization SSO authorization: {url}"
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



def _rate_limit_message(*, token_configured: bool, headers: Any) -> str:
    message = "GitHub API rate limited schema pin resolution"
    if token_configured:
        message += "; the configured token may have exhausted its rate limit"
    else:
        message += "; set GITHUB_TOKEN to increase the API rate limit"
    remaining = headers.get("X-RateLimit-Remaining") if headers is not None else None
    reset = headers.get("X-RateLimit-Reset") if headers is not None else None
    details: list[str] = []
    if remaining is not None:
        details.append(f"remaining={remaining}")
    if reset is not None:
        details.append(f"reset={reset}")
    if details:
        message += " (" + ", ".join(details) + ")"
    return message + "; retry later"


__all__ = [
    "GitHubKubeconformSchemaNotFoundError",
    "GitHubKubeconformSchemaSource",
    "GitHubKubeconformSchemaSourceEnvironmentError",
    "GitHubKubeconformSchemaSourceError",
    "GitHubKubeconformSchemaSourceIntegrityError",
]
