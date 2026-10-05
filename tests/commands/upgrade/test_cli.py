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
    outcome=UpgradeStatus.PR_OPEN,
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
    """A workspace one level below tmp_path, so Renovate's data file can sit outside it."""
    root = tmp_path / "repo"
    root.mkdir()
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

def test_upgrade_json_is_byte_stable_and_flags_become_the_request(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", "--path", "charts/loki", "--dry-run", "--output", "json")

    assert result.exit_code == 0
    # Byte-identical: `-o json` is read by CI steps and jq, so key order,
    # separators and the trailing newline are all part of the contract.
    assert result.stdout == (
        '{"base":null,"branch":"renovate/loki","chart":"loki",'
        '"current_wrapper_version":"1.2.3",'
        '"diagnostics":["registry lookup retried"],"outcome":"pr_open",'
        '"path":"charts/loki","proposed_wrapper_version":"1.2.4",'
        '"pull_request":{"number":7,"url":"https://example.test/pull/7"},'
        '"repository":"owner/repository"}\n'
    )
    (request,) = fake.requests
    assert request.chart_path == Path("charts/loki")
    assert request.dry_run is True


def test_upgrade_table_renders_every_field_in_a_fixed_order(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", "--path", "charts/loki", "--output", "table")

    assert result.exit_code == 0
    assert result.stdout == (
        "repository: owner/repository\n"
        "base: -\n"
        "chart: loki\n"
        "path: charts/loki\n"
        "current wrapper version: 1.2.3\n"
        "proposed wrapper version: 1.2.4\n"
        "branch: renovate/loki\n"
        "outcome: pr_open\n"
        "pull request: #7 https://example.test/pull/7\n"
        "diagnostics:\n"
        "- registry lookup retried\n"
    )


def test_upgrade_with_nothing_to_propose_reports_null_proposal_and_pull_request(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_upgrade(
        monkeypatch,
        UpgradeResult(
            chart="loki",
            chart_path=repo / "charts" / "loki",
            current_version="1.2.3",
            proposed_version=None,
            branch=None,
            group="chart-manager:loki",
            outcome=UpgradeStatus.NO_CHANGES,
            repository="owner/repository",
        ),
    )

    result = cli("chart", "upgrade", "--path", "charts/loki", "-o", "json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["path"] == (repo / "charts" / "loki").as_posix()
    assert payload["proposed_wrapper_version"] is None
    assert payload["pull_request"] is None
    assert payload["diagnostics"] == []
    assert payload["outcome"] == "no_changes"


def test_a_pull_request_without_a_number_is_still_reported(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reporting `"pull_request": null` for a run that opened one would be a lie."""
    _fake_upgrade(monkeypatch, UpgradeResult(**{**vars(OPENED), "pr_number": None}))

    as_json = cli("chart", "upgrade", "--path", "charts/loki", "-o", "json")
    as_table = cli("chart", "upgrade", "--path", "charts/loki", "-o", "table")

    assert json.loads(as_json.stdout)["pull_request"] == {
        "url": "https://example.test/pull/7",
        "number": None,
    }
    assert "pull request: https://example.test/pull/7\n" in as_table.stdout


def test_the_chart_argument_names_its_chart_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "charts" / "loki").mkdir(parents=True)
    (repo / "charts" / "loki" / "Chart.yaml").write_text(
        "apiVersion: v2\nname: loki\nversion: 1.2.3\n", encoding="utf-8"
    )
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", "loki")

    assert result.exit_code == 0
    assert fake.requests[0].chart_path == Path("charts/loki")


@pytest.mark.parametrize(
    "argv", [(), ("loki", "--path", "charts/loki")], ids=["neither", "both"]
)
def test_upgrade_needs_exactly_one_chart(
    repo: Path, monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]
) -> None:
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", *argv)

    # A ChartManagerError, which `main()` turns into an `error:` line and exit 1.
    assert isinstance(result.exception, ChartManagerError)
    assert "name exactly one chart" in str(result.exception)
    assert fake.requests == []


def test_unknown_output_is_rejected_before_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_upgrade(monkeypatch)

    result = cli("chart", "upgrade", "--path", "charts/loki", "--output", "yaml")

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

    result = cli("upgrade-finalize", "--path", "charts/loki", "--format", "json")

    assert "upgrade-finalize" not in cli("--help").stdout
    assert result.exit_code == 0
    # Renovate's callback reads this line, so pin it byte for byte -- including
    # the keys finalize cannot populate, and its previous_version/version landing
    # on current_wrapper_version/proposed_wrapper_version.
    assert result.stdout == (
        '{"base":null,"branch":null,"chart":"loki",'
        '"current_wrapper_version":"1.2.3","diagnostics":[],'
        '"outcome":"updated","path":"charts/loki",'
        '"proposed_wrapper_version":"2.0.0","pull_request":null,'
        '"repository":null}\n'
    )
    (request,) = fake.requests
    assert request.chart_path == Path("charts/loki")
    assert request.update_data["updates"][0]["depName"] == "grafana"


def test_finalize_text_renders_the_keys_finalize_cannot_populate(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_file = repo / "renovate-data.json"
    data_file.write_text('{"updates":[]}', encoding="utf-8")
    _fake_finalize(monkeypatch, UNCHANGED)

    result = cli("upgrade-finalize", "--path", "charts/loki", "--data-file", str(data_file))

    assert result.exit_code == 0
    # An unchanged finalize still reports a version; `outcome` tells the cases apart.
    assert result.stdout == (
        "repository: -\n"
        "base: -\n"
        "chart: loki\n"
        "path: charts/loki\n"
        "current wrapper version: 1.2.3\n"
        "proposed wrapper version: 1.2.3\n"
        "branch: -\n"
        "outcome: unchanged\n"
        "pull request: -\n"
        "diagnostics:\n"
        "- none\n"
    )


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
