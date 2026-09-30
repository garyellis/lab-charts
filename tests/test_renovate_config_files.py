"""Contract tests for the repository and self-hosted Renovate config split."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict[str, object]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_repository_config_enables_only_supported_chart_managers() -> None:
    config = _load("renovate.json")

    assert config["enabledManagers"] == [
        "helmv3",
        "helm-values",
        "custom.regex",
    ]
    assert config["ignorePaths"] == []
    assert config["helm-values"] == {"managerFilePatterns": ["/(^|/)values(?:-[^/]+)?\\.ya?ml$/"]}
    assert "extends" not in config
    custom_manager = config["customManagers"][0]  # type: ignore[index]
    assert custom_manager["datasourceTemplate"] == "docker"
    assert custom_manager["managerFilePatterns"] == ["/(^|/)templates/.+\\.(?:ya?ml|tpl)$/"]
    assert "image:" in custom_manager["matchStrings"][0]
    assert "allowedCommands" not in config
    assert "repositories" not in config
    # The chart scope and its branch namespace are supplied per run through
    # `force`, which outranks this file. Setting them here as well would be a
    # second, weaker source of truth for cross-chart isolation.
    for key in ("branchPrefix", "branchPrefixOld", "includePaths", "pruneStaleBranches"):
        assert key not in config


def test_schema_updates_regenerate_only_the_workspace_and_lock() -> None:
    config = _load("renovate.json")
    schema_policy = config["customManagers"][2]  # type: ignore[index]
    schema_lock = config["customManagers"][3]  # type: ignore[index]
    rule = config["packageRules"][0]  # type: ignore[index]

    assert schema_policy["managerFilePatterns"] == ["/^\\.chart-manager/workspace\\.yaml$/"]
    assert schema_policy["depTypeTemplate"] == "schema-policy"
    assert schema_policy["datasourceTemplate"] == "github-releases"
    assert schema_policy["depNameTemplate"] == "kubernetes/kubernetes"
    assert schema_policy["extractVersionTemplate"] == "^v(?<version>.+)$"
    assert schema_lock["managerFilePatterns"] == ["/^\\.chart-manager/schemas\\.lock\\.yaml$/"]
    assert schema_lock["depTypeTemplate"] == "schema-lock"
    assert rule["matchDepTypes"] == ["schema-policy", "schema-lock"]
    assert rule["schedule"] == ["before 6am on monday"]
    assert rule["postUpgradeTasks"] == {
        "commands": ["chart-manager schemas sync --update"],
        "fileFilters": [
            ".chart-manager/workspace.yaml",
            ".chart-manager/schemas.lock.yaml",
        ],
        "executionMode": "update",
    }
    lag_rule = config["packageRules"][1]  # type: ignore[index]
    assert lag_rule["matchPackageNames"] == ["kubernetes/kubernetes"]
    assert lag_rule["minimumReleaseAge"] == "14 days"


def test_schema_regexes_extract_policy_version_and_tracking_pins() -> None:
    managers = _load("renovate.json")["customManagers"]  # type: ignore[index]
    policy_pattern = re.compile(
        managers[2]["matchStrings"][0].replace("(?<", "(?P<")  # type: ignore[index]
    )
    lock_pattern = re.compile(
        managers[3]["matchStrings"][0].replace("(?<", "(?P<")  # type: ignore[index]
    )

    policy = policy_pattern.search('    kubernetesVersion: "1.35.3"')
    assert policy is not None
    assert policy.group("currentValue") == "1.35.3"

    # Exercise the generated artifact rather than a hand-written field order:
    # the lock serializer's stable ordering is part of the Renovate contract.
    lock = (ROOT / ".chart-manager" / "schemas.lock.yaml").read_text()
    pins = [
        (match.group("depName"), match.group("currentValue"), match.group("currentDigest"))
        for match in lock_pattern.finditer(lock)
    ]
    assert {(name, track) for name, track, _digest in pins} == {
        ("datreeio/CRDs-catalog", "main"),
        ("yannh/kubernetes-json-schema", "master"),
    }
    assert all(re.fullmatch(r"[a-f0-9]{40}", digest) for _, _, digest in pins)


def test_image_regex_splits_on_the_tag_not_a_registry_port() -> None:
    pattern = _load("renovate.json")["customManagers"][0]["matchStrings"][0]  # type: ignore[index]
    # Renovate uses JS named groups; Python spells them `(?P<...>`.
    compiled = re.compile(pattern.replace("(?<", "(?P<"))

    def split(line: str) -> tuple[str, str] | None:
        match = compiled.search(line)
        return (match.group("depName"), match.group("currentValue")) if match else None

    assert split("image: docker.io/grafana/grafana:10.5.3") == (
        "docker.io/grafana/grafana",
        "10.5.3",
    )
    assert split('image: "registry.k8s.io/kubectl:v1.35.3"') == (
        "registry.k8s.io/kubectl",
        "v1.35.3",
    )
    # A mirrored image on a ported registry must resolve to the image and its
    # tag, not to the hostname and the port. Per-environment values files can
    # source the same image from different registries, so this shape is
    # expected rather than exotic.
    assert split("image: harbor.lab.local:5000/grafana/grafana:10.5.3") == (
        "harbor.lab.local:5000/grafana/grafana",
        "10.5.3",
    )


def test_global_config_is_separate_and_has_narrow_command_policy() -> None:
    config = _load("renovate-global.json")

    assert config["allowShellExecutorForPostUpgradeCommands"] is False
    assert config["onboarding"] is False
    assert config["requireConfig"] == "required"
    assert config["allowedCommands"] == [
        "^chart-manager upgrade-finalize --path "
        "(?:[A-Za-z0-9][A-Za-z0-9._-]*/)+[A-Za-z0-9][A-Za-z0-9._-]*$",
        "^chart-manager schemas sync --update$",
    ]


def test_global_command_allowlist_accepts_only_one_safe_chart_path() -> None:
    pattern = re.compile(_load("renovate-global.json")["allowedCommands"][0])  # type: ignore[index]

    assert pattern.fullmatch("chart-manager upgrade-finalize --path charts/prometheus-operator")
    assert pattern.fullmatch("chart-manager upgrade-finalize --path wrappers/team/loki")
    assert not pattern.fullmatch("chart-manager upgrade-finalize --path charts/loki && env")
    assert not pattern.fullmatch("chart-manager upgrade-finalize --path ../outside")


def test_global_command_allowlist_accepts_only_exact_schema_update() -> None:
    pattern = re.compile(_load("renovate-global.json")["allowedCommands"][1])  # type: ignore[index]

    assert pattern.fullmatch("chart-manager schemas sync --update")
    assert not pattern.fullmatch("chart-manager schemas sync")
    assert not pattern.fullmatch("chart-manager schemas sync --update && env")
    assert not pattern.fullmatch("chart-manager schemas sync --update --root /tmp/repo")


def test_global_filename_cannot_be_auto_discovered_as_repo_config() -> None:
    # Renovate auto-discovers root renovate.json. Self-hosted policy has a
    # deliberately non-standard name and is loaded only by CONFIG_FILE.
    assert (ROOT / "renovate.json").is_file()
    assert (ROOT / "renovate-global.json").is_file()
    assert not (ROOT / "config.js").exists()
