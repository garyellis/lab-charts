from __future__ import annotations

import io
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from chart_manager.integrations.kubeconform.github_schema_source import (
    GitHubKubeconformSchemaNotFoundError,
    GitHubKubeconformSchemaSource,
    GitHubKubeconformSchemaSourceEnvironmentError,
    GitHubKubeconformSchemaSourceIntegrityError,
    _GitHubRedirectHandler,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/schema.json",
        "http://raw.githubusercontent.com/schema.json",
        "https://raw.githubusercontent.com.evil.example/x",
        "file:///tmp/schema.json",
        "https://user@api.github.com/x",
        "https://api.github.com:444/x",
    ],
)
def test_source_rejects_disallowed_hosts_before_io(url: str) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("disallowed URL reached network")

    with pytest.raises(GitHubKubeconformSchemaSourceIntegrityError, match="must use HTTPS"):
        GitHubKubeconformSchemaSource(opener=unexpected)._read(url, max_bytes=100)
    with pytest.raises(GitHubKubeconformSchemaSourceIntegrityError, match="must use HTTPS"):
        _GitHubRedirectHandler().redirect_request(
            Request("https://api.github.com/repos/a/b"), None, 302, "found", {}, url
        )


def test_redirect_to_raw_github_does_not_forward_api_token() -> None:
    redirected = _GitHubRedirectHandler().redirect_request(
        Request("https://api.github.com/repos/a/b", headers={"Authorization": "Bearer secret"}),
        None,
        302,
        "found",
        {},
        "https://raw.githubusercontent.com/a/b/schema.json",
    )
    assert redirected is not None
    assert not redirected.has_header("Authorization")


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
        raise HTTPError("https://raw.githubusercontent.com", 404, "missing", {}, None)

    with pytest.raises(GitHubKubeconformSchemaNotFoundError):
        GitHubKubeconformSchemaSource(opener=missing)._read("https://raw.githubusercontent.com", max_bytes=100)

    def offline(*_args, **_kwargs):
        raise URLError("network down")

    with pytest.raises(
        GitHubKubeconformSchemaSourceEnvironmentError,
        match="unreachable",
    ):
        GitHubKubeconformSchemaSource(opener=offline)._read("https://raw.githubusercontent.com", max_bytes=100)


def test_ref_response_is_size_bounded():
    client = GitHubKubeconformSchemaSource(opener=lambda *args, **kwargs: _Response(b"123"))
    with pytest.raises(GitHubKubeconformSchemaSourceIntegrityError, match="exceeds"):
        client._read("https://api.github.com/repos/x/y", max_bytes=2)


def test_token_is_sent_only_to_github_api() -> None:
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return _Response(b'{"sha":"' + b"a" * 40 + b'"}')

    client = GitHubKubeconformSchemaSource(opener=opener, github_token="secret")
    client.resolve_ref("owner/repo", "main")
    client._read("https://raw.githubusercontent.com/x", max_bytes=100)

    assert requests[0].get_header("Authorization") == "Bearer secret"
    assert requests[1].get_header("Authorization") is None


def test_unset_token_sends_no_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("RENOVATE_TOKEN", raising=False)
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return _Response(b'{"sha":"' + b"a" * 40 + b'"}')

    GitHubKubeconformSchemaSource(opener=opener).resolve_ref("owner/repo", "main")

    assert requests[0].get_header("Authorization") is None


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("token", [None, "highly-secret-token"])
def test_github_api_rate_limit_has_actionable_diagnostic(
    status: int,
    token: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("RENOVATE_TOKEN", raising=False)

    def limited(request, **_kwargs):
        raise HTTPError(
            request.full_url,
            status,
            "limited",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "12345"},
            None,
        )

    client = GitHubKubeconformSchemaSource(opener=limited, github_token=token)
    with pytest.raises(
        GitHubKubeconformSchemaSourceEnvironmentError,
    ) as caught:
        client.resolve_ref("owner/repo", "main")

    message = str(caught.value)
    assert "rate limited" in message
    assert "remaining=0" in message
    assert "reset=12345" in message
    if token is None:
        assert "set GITHUB_TOKEN" in message
    else:
        assert "configured token" in message
        assert token not in message


def test_github_permission_denied_is_not_reported_as_rate_limit() -> None:
    def forbidden(request, **_kwargs):
        raise HTTPError(request.full_url, 403, "forbidden", {}, None)

    client = GitHubKubeconformSchemaSource(opener=forbidden, github_token="secret")
    with pytest.raises(GitHubKubeconformSchemaSourceEnvironmentError) as caught:
        client.resolve_ref("owner/repo", "main")
    assert "HTTP 403" in str(caught.value)
    assert "permissions" in str(caught.value)
    assert "rate limited" not in str(caught.value)
    assert "secret" not in str(caught.value)
