from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github, PullRequest
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.helmrelease import (
    PromoteRequest,
    PromoteService,
)

_HR_TEMPLATE = """\
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: {name}
  namespace: {ns}
spec:
  chart:
    spec:
      chart: {chart}
      version: "{version}"
      sourceRef:
        kind: HelmRepository
        name: lab-charts
        namespace: flux-system
"""

_FAKE_URL = "git@github.com:org/lab-fluxcd.git"


def _write_hr(
    repo: Path,
    rel_path: str,
    *,
    chart: str,
    version: str,
    name: str = "loki",
    ns: str = "loki",
) -> Path:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_HR_TEMPLATE.format(name=name, ns=ns, chart=chart, version=version))
    return target


def _cloner(fixture: Path, captured: list[Path] | None = None):
    def _clone(url: str, target: Path, branch: str) -> None:
        shutil.copytree(fixture, target)
        if captured is not None:
            captured.append(target)

    return _clone


class _FakeGit(Git):
    def __init__(self, root: Path) -> None:
        super().__init__(root, runner=CommandRunner())
        self.calls: list[tuple[str, ...]] = []

    def checkout_new_branch(self, branch: str, *, base: str | None = None) -> None:
        self.calls.append(("checkout", branch, base or ""))

    def add(self, paths: Sequence[Path | str]) -> None:
        self.calls.append(("add", *[str(p) for p in paths]))

    def commit(
        self, message: str, *, body: str | None = None, allow_empty: bool = False
    ) -> None:
        self.calls.append(("commit", message, body or ""))

    def push(
        self, branch: str, *, remote: str = "origin", set_upstream: bool = True
    ) -> None:
        self.calls.append(("push", remote, branch))


class _FakeGithub(Github):
    def __init__(self, repo_root: Path) -> None:
        super().__init__(repo_root, runner=CommandRunner())
        self.created: list[tuple[str, str, str, str]] = []
        self.existing_pr: PullRequest | None = None

    def find_open_pr_for_branch(
        self, branch: str, *, base: str | None = None
    ) -> PullRequest | None:
        return self.existing_pr

    def create_pr(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> PullRequest:
        self.created.append((title, body, head, base))
        return PullRequest(url="https://github.com/org/flux/pull/42", number=42)


def _service(fixture: Path) -> PromoteService:
    return PromoteService(
        git_factory=_FakeGit,
        github_factory=_FakeGithub,
        clone_fn=_cloner(fixture),
    )


def _capture_factories(fixture: Path) -> tuple[
    list[_FakeGit],
    list[_FakeGithub],
    list[Path],
    PromoteService,
]:
    gits: list[_FakeGit] = []
    ghs: list[_FakeGithub] = []
    workdirs: list[Path] = []

    def git_factory(root: Path) -> _FakeGit:
        g = _FakeGit(root)
        gits.append(g)
        return g

    def github_factory(root: Path) -> _FakeGithub:
        g = _FakeGithub(root)
        ghs.append(g)
        return g

    service = PromoteService(
        git_factory=git_factory,
        github_factory=github_factory,
        clone_fn=_cloner(fixture, workdirs),
    )
    return gits, ghs, workdirs, service


def test_promote_raises_when_chart_not_found(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.1.2")
    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="certz-manager",
        version="0.1.1",
    )

    with pytest.raises(ChartManagerError, match="certz-manager.*not found"):
        _service(fixture).promote(req)


def test_promote_no_changes_when_already_target(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.1.2")
    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="loki",
        version="0.1.2",
    )
    result = _service(fixture).promote(req)

    assert result.no_changes is True
    assert result.changed_files == []
    assert result.pull_request is None


def test_promote_dry_run_does_not_edit_or_call_git(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.1.1")
    fixture_text = (fixture / "prod/loki.yaml").read_text()

    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="loki",
        version="0.1.2",
        dry_run=True,
    )
    gits, ghs, _workdirs, service = _capture_factories(fixture)
    result = service.promote(req)

    assert result.dry_run is True
    assert result.pull_request is None
    assert result.branch == "promote/prod/loki-0.1.2"
    assert len(result.changed_files) == 1
    assert result.changed_files[0].name == "loki.yaml"
    # The fixture is the source of truth; it must remain pristine.
    assert (fixture / "prod/loki.yaml").read_text() == fixture_text
    assert gits == []
    assert ghs == []


def test_promote_opens_pr_when_drift_exists(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/a/loki.yaml", chart="loki", version="0.1.1")
    _write_hr(fixture, "prod/b/loki.yaml", chart="loki", version="0.1.1")
    _write_hr(
        fixture,
        "prod/grafana.yaml",
        chart="grafana",
        version="1.0.0",
        name="grafana",
        ns="grafana",
    )
    gits, ghs, workdirs, service = _capture_factories(fixture)
    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="loki",
        version="0.1.2",
    )
    result = service.promote(req)

    workdir = workdirs[0].resolve()
    assert result.branch == "promote/prod/loki-0.1.2"
    assert set(result.changed_files) == {
        workdir / "prod/a/loki.yaml",
        workdir / "prod/b/loki.yaml",
    }
    assert result.pull_request is not None
    assert result.pull_request.url.endswith("/42")

    git = gits[0]
    op_names = [c[0] for c in git.calls]
    assert op_names == ["checkout", "add", "commit", "push"]
    checkout = git.calls[0]
    assert checkout[1] == "promote/prod/loki-0.1.2"
    assert checkout[2] == "main"
    commit = git.calls[2]
    assert "loki" in commit[1] and "0.1.2" in commit[1]
    assert "0.1.1" in commit[2] and "loki.yaml" in commit[2]

    gh = ghs[0]
    assert len(gh.created) == 1
    title, body, head, base = gh.created[0]
    assert "loki" in title and "0.1.2" in title and "prod" in title
    assert head == "promote/prod/loki-0.1.2"
    assert base == "main"
    # PR body uses workdir-relative paths, not the temp absolute paths.
    assert "prod/a/loki.yaml" in body
    assert "prod/b/loki.yaml" in body


def test_promote_dedupes_multi_doc_drift_to_one_file(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    target = fixture / "prod/loki.yaml"
    target.parent.mkdir(parents=True)
    doc_a = _HR_TEMPLATE.format(name="loki-a", ns="loki", chart="loki", version="0.1.1")
    doc_b = _HR_TEMPLATE.format(name="loki-b", ns="loki", chart="loki", version="0.1.1")
    target.write_text(doc_a + doc_b)

    gits, _ghs, workdirs, service = _capture_factories(fixture)
    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="loki",
        version="0.1.2",
    )
    result = service.promote(req)

    workdir = workdirs[0].resolve()
    assert result.changed_files == [workdir / "prod/loki.yaml"]
    add_call = next(c for c in gits[0].calls if c[0] == "add")
    assert add_call.count(str(workdir / "prod/loki.yaml")) == 1


def test_promote_returns_existing_pr_without_mutating(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.1.1")
    fixture_text = (fixture / "prod/loki.yaml").read_text()

    pre_existing = PullRequest(url="https://github.com/org/flux/pull/7", number=7)
    gits: list[_FakeGit] = []

    def git_factory(root: Path) -> _FakeGit:
        g = _FakeGit(root)
        gits.append(g)
        return g

    def github_factory(root: Path) -> _FakeGithub:
        gh = _FakeGithub(root)
        gh.existing_pr = pre_existing
        return gh

    service = PromoteService(
        git_factory=git_factory,
        github_factory=github_factory,
        clone_fn=_cloner(fixture),
    )
    result = service.promote(
        PromoteRequest(
            flux_repo=_FAKE_URL,
            path=Path("prod/"),
            environment="prod",
            chart_name="loki",
            version="0.1.2",
        )
    )

    assert result.already_open is True
    assert result.pull_request is pre_existing
    assert result.branch == "promote/prod/loki-0.1.2"
    assert result.changed_files == []
    assert all(g.calls == [] for g in gits)
    assert (fixture / "prod/loki.yaml").read_text() == fixture_text


def test_promote_errors_on_path_traversal(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.1.1")
    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("../"),
        environment="prod",
        chart_name="loki",
        version="0.1.2",
    )
    with pytest.raises(ChartManagerError, match="escapes"):
        _service(fixture).promote(req)


def test_promote_clone_failure_surfaces_to_caller(tmp_path: Path) -> None:
    def boom(url: str, target: Path, branch: str) -> None:
        raise ChartManagerError(f"clone failed: {url}")

    service = PromoteService(
        git_factory=_FakeGit,
        github_factory=_FakeGithub,
        clone_fn=boom,
    )
    with pytest.raises(ChartManagerError, match="clone failed"):
        service.promote(
            PromoteRequest(
                flux_repo=_FAKE_URL,
                path=Path("prod/"),
                environment="prod",
                chart_name="loki",
                version="0.1.2",
            )
        )


def test_branch_and_pr_text_are_deterministic(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.1.1")
    captures: list[tuple[str, str, str, str]] = []

    def github_factory(root: Path) -> _FakeGithub:
        class _Capture(_FakeGithub):
            def create_pr(
                self,
                *,
                title: str,
                body: str,
                head: str,
                base: str,
                draft: bool = False,
            ) -> PullRequest:
                captures.append((title, body, head, base))
                return PullRequest(url="x", number=1)

        return _Capture(root)

    service = PromoteService(
        git_factory=_FakeGit,
        github_factory=github_factory,
        clone_fn=_cloner(fixture),
    )
    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="loki",
        version="0.1.2",
    )
    service.promote(req)
    assert captures[0][0] == "chore(prod): promote loki to 0.1.2"
    assert captures[0][2] == "promote/prod/loki-0.1.2"


# --- Downgrade gate -------------------------------------------------------

def _downgrade_request() -> PromoteRequest:
    return PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="loki",
        version="0.1.0",
    )


def test_downgrade_raises_without_callback(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.2.0")
    service = PromoteService(
        git_factory=_FakeGit,
        github_factory=_FakeGithub,
        clone_fn=_cloner(fixture),
    )
    with pytest.raises(ChartManagerError, match="refusing to downgrade"):
        service.promote(_downgrade_request())


def test_downgrade_proceeds_when_callback_returns_true(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.2.0")
    received: list[tuple[int, str]] = []

    def confirm(downgrades: list, target: str) -> bool:
        received.append((len(downgrades), target))
        return True

    gits, ghs = [], []

    def git_factory(root: Path) -> _FakeGit:
        g = _FakeGit(root)
        gits.append(g)
        return g

    def github_factory(root: Path) -> _FakeGithub:
        g = _FakeGithub(root)
        ghs.append(g)
        return g

    service = PromoteService(
        git_factory=git_factory,
        github_factory=github_factory,
        clone_fn=_cloner(fixture),
        confirm_downgrade=confirm,
    )
    result = service.promote(_downgrade_request())

    assert received == [(1, "0.1.0")]
    assert result.aborted is False
    assert result.pull_request is not None
    assert len(result.downgrades) == 1
    assert result.downgrades[0].current_version == "0.2.0"
    assert ghs[0].created  # PR was actually created


def test_downgrade_aborts_when_callback_returns_false(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.2.0")
    gits: list[_FakeGit] = []
    ghs: list[_FakeGithub] = []

    def git_factory(root: Path) -> _FakeGit:
        g = _FakeGit(root)
        gits.append(g)
        return g

    def github_factory(root: Path) -> _FakeGithub:
        g = _FakeGithub(root)
        ghs.append(g)
        return g

    service = PromoteService(
        git_factory=git_factory,
        github_factory=github_factory,
        clone_fn=_cloner(fixture),
        confirm_downgrade=lambda _d, _t: False,
    )
    result = service.promote(_downgrade_request())

    assert result.aborted is True
    assert result.pull_request is None
    assert result.changed_files == []
    assert len(result.downgrades) == 1
    # No git or gh side effects.
    assert all(g.calls == [] for g in gits)
    assert all(g.created == [] for g in ghs)


def test_downgrade_skips_callback_in_dry_run(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.2.0")
    called = False

    def confirm(_d: list, _t: str) -> bool:
        nonlocal called
        called = True
        return False

    service = PromoteService(
        git_factory=_FakeGit,
        github_factory=_FakeGithub,
        clone_fn=_cloner(fixture),
        confirm_downgrade=confirm,
    )
    req = PromoteRequest(
        flux_repo=_FAKE_URL,
        path=Path("prod/"),
        environment="prod",
        chart_name="loki",
        version="0.1.0",
        dry_run=True,
    )
    result = service.promote(req)

    assert called is False
    assert result.dry_run is True
    assert len(result.downgrades) == 1


def test_upgrade_does_not_invoke_callback(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="0.1.0")
    called = False

    def confirm(_d: list, _t: str) -> bool:
        nonlocal called
        called = True
        return True

    service = PromoteService(
        git_factory=_FakeGit,
        github_factory=_FakeGithub,
        clone_fn=_cloner(fixture),
        confirm_downgrade=confirm,
    )
    result = service.promote(
        PromoteRequest(
            flux_repo=_FAKE_URL,
            path=Path("prod/"),
            environment="prod",
            chart_name="loki",
            version="0.2.0",
        )
    )

    assert called is False
    assert result.pull_request is not None
    assert result.downgrades == []


def test_non_semver_current_version_does_not_gate(tmp_path: Path) -> None:
    # `version: "latest"` can't be compared; the service must not block.
    fixture = tmp_path / "fixture"
    _write_hr(fixture, "prod/loki.yaml", chart="loki", version="latest")
    service = PromoteService(
        git_factory=_FakeGit,
        github_factory=_FakeGithub,
        clone_fn=_cloner(fixture),
    )
    result = service.promote(
        PromoteRequest(
            flux_repo=_FAKE_URL,
            path=Path("prod/"),
            environment="prod",
            chart_name="loki",
            version="0.1.0",
        )
    )
    assert result.pull_request is not None
    assert result.downgrades == []
