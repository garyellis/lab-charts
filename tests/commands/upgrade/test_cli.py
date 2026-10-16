"""`chart upgrade` and the hidden `upgrade-finalize`: flags, output and exit codes, with `run` faked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands.upgrade import FinalizeResult, UpgradeResult, UpgradeStatus
from chart_manager.commands.upgrade import cli as upgrade_cli
from chart_manager.plumbing.errors import ChartManagerError
from tests.conftest import cli, write_workspace

_DATA_ENV = "RENOVATE_POST_UPGRADE_COMMAND_DATA_FILE"

OPENED = UpgradeResult(
    chart="loki",
    chart_path=Path("charts/loki"),
    current_version="1.2.3",
    proposed_version="1.2.4",
    branch="renovate/loki",
    group="chart-manager:loki",
    status=UpgradeStatus.PR_OPEN,
    diagnostics=("registry lookup retried",),
    pr_url="https://example.test/pull/7",
    pr_number=7,
    repository="owner/repository",
)
UPDATED = FinalizeResult(
    chart="loki", previous_version="1.2.3", version="2.0.0", changed=True
)
UNCHANGED = FinalizeResult(
    chart="loki", previous_version="1.2.3", version="1.2.3", changed=False
)


class FakeRun:
    """Stands in for `run` or `finalize.run`: records each request and answers with `result`."""

    def __init__(self, result: UpgradeResult | FinalizeResult) -> None:
        self.result = result
        self.requests: list[Any] = []

    def __call__(self, request: object, **_: object) -> UpgradeResult | FinalizeResult:
        self.requests.append(request)
        return self.result


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with charts/loki, one level below tmp_path so Renovate's data file can sit outside it."""
    root = tmp_path / "repo"
    (root / "charts" / "loki").mkdir(parents=True)
    (root / "charts" / "loki" / "Chart.yaml").write_text(
        "apiVersion: v2\nname: loki\nversion: 1.2.3\n", encoding="utf-8"
    )
    write_workspace(root)
    monkeypatch.chdir(root)
    return root


def _fake_upgrade(monkeypatch: pytest.MonkeyPatch, result: UpgradeResult = OPENED) -> FakeRun:
    fake = FakeRun(result)
    monkeypatch.setattr(upgrade_cli, "run", fake)
    return fake


def _fake_finalize(monkeypatch: pytest.MonkeyPatch, result: FinalizeResult = UPDATED) -> FakeRun:
    fake = FakeRun(result)
    monkeypatch.setattr(upgrade_cli.finalize, "run", fake)
    return fake


# ----- chart upgrade --------------------------------------------------------

def test_upgrade_json_is_the_result_and_flags_become_the_request(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", "charts/loki", "--dry-run", "--output", "json")

    assert (result.exit_code, json.loads(result.stdout)) == (
        0,
        {
            "branch": "renovate/loki",
            "chart": "loki",
            "chart_path": "charts/loki",
            "current_version": "1.2.3",
            "diagnostics": ["registry lookup retried"],
            "group": "chart-manager:loki",
            "pr_number": 7,
            "pr_url": "https://example.test/pull/7",
            "proposed_version": "1.2.4",
            "repository": "owner/repository",
            "status": "pr_open",
        },
    )
    (request,) = fake.requests
    assert request.chart.path == repo / "charts" / "loki"
    assert request.dry_run is True


def test_upgrade_table_renders_every_field(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_upgrade(monkeypatch, UpgradeResult(**{**vars(OPENED), "pr_number": None}))

    result = cli("chart", "upgrade", "charts/loki", "--output", "table")

    assert result.exit_code == 0
    assert result.stdout == (
        "chart: loki\n"
        "chart_path: charts/loki\n"
        "current_version: 1.2.3\n"
        "proposed_version: 1.2.4\n"
        "branch: renovate/loki\n"
        "group: chart-manager:loki\n"
        "status: pr_open\n"
        "diagnostics:\n"
        "- registry lookup retried\n"
        "repository: owner/repository\n"
        "pr_url: https://example.test/pull/7\n"
        "pr_number: -\n"
    )


def test_an_unknown_pull_request_status_exits_as_a_tool_failure_and_still_reports(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch was pushed but the PR lookup failed: CI must not read that as a clean run."""
    _fake_upgrade(
        monkeypatch, UpgradeResult(**{**vars(OPENED), "status": UpgradeStatus.STATUS_UNKNOWN})
    )

    result = cli("chart", "upgrade", "charts/loki", "-o", "json")

    assert result.exit_code == 4
    assert '"status": "status_unknown"' in result.stdout


@pytest.mark.parametrize("chart", ["loki", "charts/loki"], ids=["name", "path"])
def test_the_chart_argument_takes_a_name_or_a_repository_relative_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch, chart: str
) -> None:
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", chart)

    assert result.exit_code == 0
    assert fake.requests[0].chart.path == repo / "charts" / "loki"


def test_upgrade_without_a_chart_is_a_usage_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", "--dry-run")

    assert result.exit_code == 2
    assert "Missing argument 'CHART'" in result.output
    assert fake.requests == []


def test_unknown_output_is_rejected_before_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", "charts/loki", "--output", "yaml")

    assert result.exit_code == 2
    assert "unknown output: yaml" in result.output
    assert fake.requests == []


# ----- upgrade-finalize -----------------------------------------------------

def test_finalize_is_hidden_and_reads_callback_data_from_outside_the_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Renovate writes the data file in its own temporary directory.
    data_file = repo.parent / "renovate-data.json"
    data_file.write_text(
        '{"updates":[{"depName":"grafana","currentValue":"1.0.0",'
        '"newValue":"2.0.0","datasource":"docker"}]}',
        encoding="utf-8",
    )
    fake = _fake_finalize(monkeypatch)
    monkeypatch.setenv(_DATA_ENV, str(data_file))

    result = cli("upgrade-finalize", "--path", "charts/loki", "-o", "json")

    assert "upgrade-finalize" not in cli("--help").stdout
    assert (result.exit_code, json.loads(result.stdout)) == (
        0,
        {"changed": True, "chart": "loki", "previous_version": "1.2.3", "version": "2.0.0"},
    )
    (request,) = fake.requests
    assert request.chart.path == repo / "charts" / "loki"
    assert request.update_data["updates"][0]["depName"] == "grafana"


def test_finalize_table_renders_every_field(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_file = repo / "renovate-data.json"
    data_file.write_text('{"updates":[]}', encoding="utf-8")
    _fake_finalize(monkeypatch, UNCHANGED)

    result = cli(
        "upgrade-finalize", "--path", "charts/loki", "--data-file", str(data_file), "-o", "table"
    )

    assert result.exit_code == 0
    assert result.stdout == "chart: loki\nprevious_version: 1.2.3\nversion: 1.2.3\nchanged: False\n"


def _symlinked(repo: Path) -> Path:
    target = repo / "renovate-data.json"
    target.write_text('{"updates":[]}', encoding="utf-8")
    link = repo / "link.json"
    link.symlink_to(target)
    return link


def _oversized(repo: Path) -> Path:
    data = repo / "renovate-data.json"
    data.write_text('{"padding":"' + "x" * (1024 * 1024) + '"}', encoding="utf-8")
    return data


@pytest.mark.parametrize(
    ("data_file", "message"),
    [
        (None, "--data-file is required"),
        (_symlinked, "must not be a symlink"),
        (_oversized, "safety limit"),
    ],
    ids=["missing", "symlink", "oversized"],
)
def test_finalize_refuses_unsafe_callback_data_before_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch, data_file: object, message: str
) -> None:
    fake = _fake_finalize(monkeypatch)
    argv = ["upgrade-finalize", "--path", "charts/loki"]
    if callable(data_file):
        argv += ["--data-file", str(data_file(repo))]

    result = cli(*argv)

    assert isinstance(result.exception, ChartManagerError)
    assert message in str(result.exception)
    assert fake.requests == []


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("charts/linked", "upgrade path must not contain symlinks: {repo}/charts/linked"),
        ("linked", "chart not found: {repo}/linked"),
    ],
    ids=["symlink", "bare-name"],
)
def test_finalize_refuses_a_symlinked_or_bare_chart_path_before_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch, path: str, message: str
) -> None:
    (repo / "charts" / "linked").symlink_to(repo / "charts" / "loki", target_is_directory=True)
    data_file = repo / "renovate-data.json"
    data_file.write_text('{"updates":[]}', encoding="utf-8")
    fake = _fake_finalize(monkeypatch)

    result = cli("upgrade-finalize", "--path", path, "--data-file", str(data_file))

    assert str(result.exception) == message.format(repo=repo)
    assert fake.requests == []
