from __future__ import annotations

import pytest

from chart_manager.services.tools import install as deps_install
from tests.conftest import FakeCommandRunner, Reply

# --- install_one ------------------------------------------------------


@pytest.mark.parametrize(
    "tool,versions",
    [
        ("helm", deps_install.HELM_VERSIONS),
        ("kubeconform", deps_install.KUBECONFORM_VERSIONS),
        ("kyverno", deps_install.KYVERNO_VERSIONS),
        ("uv", deps_install.UV_VERSIONS),
    ],
)
def test_install_one_invokes_mise_for_every_pinned_version(
    tool: str, versions: tuple[str, ...]
) -> None:
    runner = FakeCommandRunner()

    results = deps_install.install_one(runner, tool)

    assert [r.version for r in results] == list(versions)
    assert all(r.success for r in results)
    assert all(r.tool == tool for r in results)
    assert runner.calls == [
        ("mise", "install", f"{tool}@{v}") for v in versions
    ]


def test_install_one_unknown_tool_raises() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        deps_install.install_one(FakeCommandRunner(), "terraform")


def test_install_one_failure_warns_with_release_url_and_does_not_raise() -> None:
    # kyverno currently pins a single version; force that single call to fail.
    runner = FakeCommandRunner().script(Reply(returncode=1))
    warnings: list[str] = []

    results = deps_install.install_one(runner, "kyverno", on_warn=warnings.append)

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].tool == "kyverno"
    assert results[0].detail is not None
    assert len(warnings) == 1
    msg = warnings[0]
    assert "kyverno@" in msg
    assert deps_install.KYVERNO_VERSIONS[0] in msg
    assert "github.com/kyverno/kyverno/releases/tag/v" in msg


def test_install_one_uv_url_omits_v_prefix() -> None:
    """uv release tags don't carry the `v` prefix — guard against regression."""
    runner = FakeCommandRunner().script(Reply(returncode=1))
    warnings: list[str] = []

    deps_install.install_one(runner, "uv", on_warn=warnings.append)

    assert "github.com/astral-sh/uv/releases/tag/" in warnings[0]
    # The line carries `tag/<version>` (no `v` prefix).
    expected = f"tag/{deps_install.UV_VERSIONS[0]}"
    assert expected in warnings[0]


# --- install_all ------------------------------------------------------


def test_install_all_invokes_every_tool_and_version() -> None:
    runner = FakeCommandRunner()

    results = deps_install.install_all(runner)

    expected_calls: list[tuple[str, ...]] = []
    for tool, versions in (
        ("helm", deps_install.HELM_VERSIONS),
        ("kubeconform", deps_install.KUBECONFORM_VERSIONS),
        ("kyverno", deps_install.KYVERNO_VERSIONS),
        ("uv", deps_install.UV_VERSIONS),
    ):
        for v in versions:
            expected_calls.append(("mise", "install", f"{tool}@{v}"))

    assert runner.calls == expected_calls
    assert all(r.success for r in results)
    assert len(results) == len(expected_calls)


def test_install_all_partial_failure_aggregates_and_continues() -> None:
    # Fail the very first call (helm@4.1.3) — every later call should still run.
    total_versions = (
        len(deps_install.HELM_VERSIONS)
        + len(deps_install.KUBECONFORM_VERSIONS)
        + len(deps_install.KYVERNO_VERSIONS)
        + len(deps_install.UV_VERSIONS)
    )
    runner = FakeCommandRunner().script(Reply(returncode=1))
    warnings: list[str] = []

    results = deps_install.install_all(runner, on_warn=warnings.append)

    assert len(runner.calls) == total_versions
    assert results[0].success is False
    assert results[0].tool == "helm"
    assert all(r.success for r in results[1:])
    assert len(warnings) == 1
