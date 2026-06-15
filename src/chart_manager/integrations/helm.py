from __future__ import annotations

import threading
from functools import cache
from pathlib import Path

import yaml

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class Helm:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        version: str | None = None,
        binary: str | Path | None = None,
        verbose: bool = True,
        timeout: float | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self._helm_bin = _resolve(self.runner, version, binary)
        # verbose=True preserves the pre-existing stream-to-terminal contract
        # for kind test / ci install callers. validate run constructs with
        # verbose=False so concurrent helm invocations don't interleave
        # stdout/stderr into an unreadable mess.
        self.verbose = verbose
        # Per-subprocess wall-clock cap for all helm invocations on this
        # instance. None = unbounded (legacy behavior). Validate sets this
        # from --row-timeout so a hung helm template doesn't pin a worker.
        # dependency_update's own `timeout=` kwarg takes precedence when set.
        self.timeout = timeout
        # Per-chart dedupe for `helm dependency update`. Same Helm instance
        # validating one chart across 5 envs in parallel must only fetch
        # deps once. Lock is held across the subprocess so a concurrent
        # caller waits for the first update to finish before proceeding
        # to `helm template`.
        self._deps_updated: set[Path] = set()
        self._deps_updated_lock = threading.Lock()

    def dependency_update(self, chart_path: Path, *, timeout: float | None = None) -> None:
        resolved = chart_path.resolve()
        with self._deps_updated_lock:
            if resolved in self._deps_updated:
                return
            self.runner.run(
                [self._helm_bin, "dependency", "update", str(chart_path)],
                capture=not self.verbose,
                timeout=timeout,
            )
            self._deps_updated.add(resolved)

    def lint(self, chart_path: Path, values: list[Path]) -> None:
        # See note in upgrade_install on --skip-schema-validation; we use
        # null overrides in values-ci.yaml to wipe inherited map keys past
        # strict subchart schemas.
        args = [self._helm_bin, "lint", str(chart_path), "--skip-schema-validation"]
        args.extend(_values_args(values))
        self.runner.run(args, capture=not self.verbose, timeout=self.timeout)

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
            self._helm_bin,
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
        self.runner.run(args, capture=not self.verbose, timeout=self.timeout)

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
            self._helm_bin,
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
        self.runner.run(args, capture=not self.verbose, timeout=self.timeout)

    def template(
        self,
        release: str,
        chart_ref: str | Path,
        *,
        namespace: str,
        output_dir: Path,
        values: list[Path] | None = None,
        sets: dict[str, str] | None = None,
        api_versions: list[str] | None = None,
        kube_version: str | None = None,
        skip_tests: bool = True,
    ) -> Path:
        # Resolve to absolute up-front so the path in error messages is
        # actionable from any cwd (engineers need to be able to `ls` it).
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        if _is_local_chart(chart_ref) and _chart_has_dependencies(Path(chart_ref)):
            self.dependency_update(Path(chart_ref))

        base_args = [
            self._helm_bin,
            "template",
            release,
            str(chart_ref),
            "--namespace",
            namespace,
            "--output-dir",
            str(output_dir),
        ]
        base_args.extend(_values_args(values or []))
        base_args.extend(_set_args(sets or {}))
        for api_version in api_versions or []:
            base_args.extend(["--api-versions", api_version])
        if kube_version is not None:
            base_args.extend(["--kube-version", kube_version])
        if skip_tests:
            base_args.append("--skip-tests")

        # Deliberately NOT passing --skip-schema-validation: at template time
        # we want subchart schema errors to surface as render failures rather
        # than be silently masked. lint/upgrade_install skip them for
        # documented istio/values-overlay reasons that don't apply here.
        # verbose=True streams to terminal for interactive debugging; the
        # parallel runner sets verbose=False so 8 concurrent helms don't
        # produce interleaved garbage.
        result = self.runner.run(
            base_args, check=False, capture=not self.verbose, timeout=self.timeout
        )
        if result.returncode == 0:
            return output_dir

        debug_args = [*base_args, "--debug"]
        # Always capture the debug rerun's output so we can embed it in the
        # raised error (verbose mode still streams the first attempt above).
        debug_result = self.runner.run(debug_args, check=False, capture=True)
        stderr = (debug_result.stderr or result.stderr or "").strip()
        raise ExternalCommandError(
            f"helm template failed for {release} ({chart_ref}); "
            f"rendered (partial) output at: {output_dir}\n{stderr}"
        )

    def test(self, release: str, *, namespace: str, timeout: str = "10m") -> None:
        self.runner.run(
            [self._helm_bin, "test", release, "--namespace", namespace, "--timeout", timeout],
            capture=not self.verbose,
        )

    def status(self, release: str, *, namespace: str) -> str:
        result = self.runner.run(
            [self._helm_bin, "status", release, "--namespace", namespace],
            check=False,
        )
        return result.stdout + result.stderr


def _resolve(
    runner: CommandRunner,
    version: str | None,
    binary: str | Path | None,
) -> str:
    if binary is not None:
        return str(binary)
    if version is None:
        return "helm"
    return _resolve_via_mise(runner, version)


@cache
def _resolve_via_mise(runner: CommandRunner, version: str) -> str:
    # CPython's lru_cache is protected by a C-level lock, so this is safe
    # to call from concurrent worker threads. Keyed positionally on
    # (runner, version): `runner` hashes by identity, which is exactly what
    # we want — instances sharing one CommandRunner share the cache; ones
    # with their own runner each pay a one-shot `mise where`.
    result = runner.run(["mise", "where", f"helm@{version}"], check=True)
    return f"{result.stdout.strip()}/bin/helm"


def _is_local_chart(chart_ref: str | Path) -> bool:
    ref = str(chart_ref)
    if ref.startswith(("oci://", "http://", "https://")):
        return False
    return Path(ref).exists()


def _chart_has_dependencies(chart_path: Path) -> bool:
    chart_yaml = chart_path / "Chart.yaml"
    if not chart_yaml.is_file():
        return False
    try:
        data = yaml.safe_load(chart_yaml.read_text()) or {}
    except (yaml.YAMLError, OSError):
        # Defer the actual error to `helm template`, which will surface a
        # clear chart-loading message. We only return False so we don't
        # spuriously call `helm dependency update` on an unparseable chart.
        return False
    if not isinstance(data, dict):
        return False
    deps = data.get("dependencies") or []
    return isinstance(deps, list) and bool(deps)


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
