"""Focused tests for the self-hosted Renovate subprocess adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.integrations.renovate import Renovate, RenovateRequest
from chart_manager.plumbing.errors import (
    ChartManagerError,
    ExternalCommandError,
    MissingToolError,
)
from tests.conftest import FakeCommandRunner


def _config(root: Path, name: str = "renovate-global.json") -> Path:
    path = root / name
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_run_scopes_argv_cwd_and_all_config_layers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    global_config = _config(tmp_path)
    additional_config = _config(tmp_path, ".chart-renovate.json")
    overlay = {
        "includePaths": ["charts/loki/**"],
        "enabledManagers": ["helmv3", "helm-values", "custom.regex"],
        "packageRules": [
            {
                "matchManagers": ["helmv3", "helm-values", "custom.regex"],
                "groupName": "loki dependencies",
                "groupSlug": "loki-dependencies",
                "separateMajorMinor": False,
                "separateMinorPatch": False,
            }
        ],
        "postUpgradeTasks": {
            "commands": ["chart-manager upgrade-finalize --path charts/loki"],
            "dataFileTemplate": '{"upgrades":{{{toJSON upgrades}}}}',
            "executionMode": "branch",
            "fileFilters": [
                "charts/loki/Chart.yaml",
                "charts/loki/Chart.lock",
                "charts/loki/CHANGELOG.md",
            ],
        },
    }
    runner = FakeCommandRunner(stdout="done\n")

    with caplog.at_level("DEBUG"):
        result = Renovate(runner=runner, timeout=45).run(
            RenovateRequest(
                repo_root=tmp_path,
                repository="garyellis/lab-charts",
                global_config_path=Path("renovate-global.json"),
                additional_config_path=additional_config,
                runtime_overlay=overlay,
                dry_run="full",
                token="secret-token",
            )
        )

    assert result.ok is True
    assert result.stdout == "done\n"
    record = runner.records[0]
    assert record.args == ("renovate", "garyellis/lab-charts")
    assert record.cwd == tmp_path.resolve()
    assert record.check is False
    assert record.timeout == 45
    assert record.env is not None
    assert record.env["RENOVATE_CONFIG_FILE"] == str(global_config.resolve())
    assert record.env["RENOVATE_ADDITIONAL_CONFIG_FILE"] == str(additional_config.resolve())
    assert json.loads(record.env["RENOVATE_CONFIG"]) == overlay
    assert record.env["RENOVATE_DRY_RUN"] == "full"
    assert record.env["RENOVATE_TOKEN"] == "secret-token"
    assert "Starting Renovate for garyellis/lab-charts (dry-run=full)" in caplog.text
    assert "renovate> done" in caplog.text
    assert "Authentication: configured" in caplog.text


def test_token_is_environment_only_and_excluded_from_repr(tmp_path: Path) -> None:
    global_config = _config(tmp_path)
    request = RenovateRequest(
        repo_root=tmp_path,
        repository="owner/repo",
        global_config_path=global_config,
        token="super-secret",
    )
    runner = FakeCommandRunner(
        returncode=1,
        stdout="debug token=super-secret",
        stderr="authentication failed: super-secret",
    )

    result = Renovate(runner=runner).run(request)

    assert "super-secret" not in repr(request)
    assert all("super-secret" not in arg for arg in runner.calls[0])
    assert "super-secret" not in repr(result)
    assert result.stdout == "debug token=***"
    assert result.returncode == 1
    assert result.stderr == "authentication failed: ***"


def test_nonzero_exit_is_a_result_for_service_owned_reporting(tmp_path: Path) -> None:
    runner = FakeCommandRunner(returncode=2, stdout="partial", stderr="bad config")

    result = Renovate(runner=runner).run(
        RenovateRequest(
            repo_root=tmp_path,
            repository="owner/repo",
            global_config_path=_config(tmp_path),
        )
    )

    assert result.ok is False
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "partial",
        "bad config",
    )


def test_optional_environment_values_are_not_injected(tmp_path: Path) -> None:
    runner = FakeCommandRunner()

    Renovate(runner=runner).run(
        RenovateRequest(
            repo_root=tmp_path,
            repository="owner/repo",
            global_config_path=_config(tmp_path),
        )
    )

    assert runner.records[0].env == {
        "RENOVATE_CONFIG_FILE": str((tmp_path / "renovate-global.json").resolve()),
        "RENOVATE_CONFIG": "{}",
    }


@pytest.mark.parametrize(
    ("repository", "message"),
    [
        ("", "slash-separated repository slug"),
        ("repo", "slash-separated repository slug"),
        ("--token=secret", "slash-separated repository slug"),
        ("owner/repo name", "slash-separated repository slug"),
    ],
)
def test_repository_slug_is_validated_before_execution(
    tmp_path: Path, repository: str, message: str
) -> None:
    runner = FakeCommandRunner()

    with pytest.raises(ChartManagerError, match=message):
        Renovate(runner=runner).run(
            RenovateRequest(
                repo_root=tmp_path,
                repository=repository,
                global_config_path=_config(tmp_path),
            )
        )

    assert runner.calls == []


def test_an_absent_binary_names_itself_and_its_remediation(tmp_path: Path) -> None:
    class MissingBinaryRunner:
        def run(self, *args: object, **kwargs: object) -> object:
            raise MissingToolError("required tool not found on PATH: renovate")

    request = RenovateRequest(
        repo_root=tmp_path,
        repository="owner/repo",
        global_config_path=_config(tmp_path),
    )

    with pytest.raises(MissingToolError) as excinfo:
        Renovate(runner=MissingBinaryRunner()).run(request)

    message = str(excinfo.value)
    assert "renovate is not installed" in message
    assert "npm install -g renovate" in message
    assert "chart-manager doctor" in message


def test_missing_config_and_invalid_overlay_use_expected_error_hierarchy(
    tmp_path: Path,
) -> None:
    renovate = Renovate(runner=FakeCommandRunner())
    with pytest.raises(ChartManagerError, match="global config is not a file"):
        renovate.run(
            RenovateRequest(
                repo_root=tmp_path,
                repository="owner/repo",
                global_config_path=Path("missing.json"),
            )
        )

    config = _config(tmp_path)
    with pytest.raises(ChartManagerError, match="not JSON serializable"):
        renovate.run(
            RenovateRequest(
                repo_root=tmp_path,
                repository="owner/repo",
                global_config_path=config,
                runtime_overlay={"bad": object()},
            )
        )


def test_validate_config_uses_repo_semantics_and_standard_failure_plumbing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "renovate.json")
    runner = FakeCommandRunner(returncode=1, stderr="invalid setting")

    with pytest.raises(ExternalCommandError, match="invalid setting"):
        Renovate(runner=runner).validate_config(
            [Path("renovate.json")],
            repo_root=tmp_path,
            global_config=False,
        )

    assert runner.calls[0] == (
        "renovate-config-validator",
        "--strict",
        "--no-global",
        str(config.resolve()),
    )
    assert runner.records[0].cwd == tmp_path.resolve()


def test_validate_global_config_omits_no_global_switch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = FakeCommandRunner(stdout="Config validated successfully")

    result = Renovate(runner=runner).validate_config(
        [config],
        repo_root=tmp_path,
        global_config=True,
        strict=False,
    )

    assert result.ok is True
    assert runner.calls[0] == ("renovate-config-validator", str(config.resolve()))
