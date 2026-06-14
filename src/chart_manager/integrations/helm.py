from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner


class Helm:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def dependency_update(self, chart_path: Path) -> None:
        self.runner.run(["helm", "dependency", "update", str(chart_path)], capture=False)

    def lint(self, chart_path: Path, values: list[Path]) -> None:
        # See note in upgrade_install on --skip-schema-validation; we use
        # null overrides in values-ci.yaml to wipe inherited map keys past
        # strict subchart schemas.
        args = ["helm", "lint", str(chart_path), "--skip-schema-validation"]
        args.extend(_values_args(values))
        self.runner.run(args, capture=False)

    def upgrade_install(
        self,
        release: str,
        chart_ref: str | Path,
        *,
        namespace: str,
        values: list[Path] | None = None,
        sets: dict[str, str] | None = None,
        timeout: str = "10m",
        wait: bool = True,
    ) -> None:
        args = [
            "helm",
            "upgrade",
            "--install",
            release,
            str(chart_ref),
            "--namespace",
            namespace,
            "--create-namespace",
            "--timeout",
            timeout,
            # Subchart schemas (notably the istio gateway/istiod charts)
            # forbid `null` for map-typed keys, which prevents wrapper
            # values-<env>.yaml overlays from wiping inherited keys via
            # Helm's deep-merge. We trust wrapper values files to be
            # well-formed and let kube-apiserver do final validation.
            "--skip-schema-validation",
            # istio-base installs ValidatingWebhookConfiguration/istiod-default-validator
            # with failurePolicy=Ignore; istiod's pilot-discovery then takes SSA
            # ownership of that field at runtime, flipping it to Fail. Without
            # this flag, every subsequent `helm upgrade istio-base` fails with
            # an SSA conflict against pilot-discovery.
            "--force-conflicts",
        ]
        if wait:
            args.append("--wait")
        args.extend(_values_args(values or []))
        args.extend(_set_args(sets or {}))
        self.runner.run(args, capture=False)

    def upgrade(
        self,
        release: str,
        chart_ref: str | Path,
        *,
        namespace: str,
        values: list[Path] | None = None,
        timeout: str = "10m",
        wait: bool = True,
    ) -> None:
        args = [
            "helm",
            "upgrade",
            release,
            str(chart_ref),
            "--namespace",
            namespace,
            "--timeout",
            timeout,
        ]
        if wait:
            args.append("--wait")
        args.extend(_values_args(values or []))
        self.runner.run(args, capture=False)

    def test(self, release: str, *, namespace: str, timeout: str = "10m") -> None:
        self.runner.run(
            ["helm", "test", release, "--namespace", namespace, "--timeout", timeout],
            capture=False,
        )

    def status(self, release: str, *, namespace: str) -> str:
        result = self.runner.run(
            ["helm", "status", release, "--namespace", namespace],
            check=False,
        )
        return result.stdout + result.stderr


def _values_args(values: list[Path]) -> list[str]:
    args: list[str] = []
    for value in values:
        args.extend(["--values", str(value)])
    return args


def _set_args(sets: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in sets.items():
        args.extend(["--set", f"{key}={value}"])
    return args
