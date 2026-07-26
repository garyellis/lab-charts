"""Thin wrapper around the `helm` CLI: install/upgrade, template, lint, test, list."""

from __future__ import annotations

import json
import threading
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from weakref import WeakKeyDictionary

from chart_manager.plumbing.commands import CommandResult, CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.services.domain.chart_deps import (
    chart_has_dependencies,
    deps_are_fresh,
    is_local_chart,
)


@dataclass(frozen=True)
class ReleaseInfo:
    """A single helm release as reported by `helm list -o json`.

    Only the fields we actually consume are surfaced; helm's JSON output
    carries more (chart, app_version, updated) but those aren't load-bearing
    for the install-skip / status-check use cases this dataclass exists for.
    """

    name: str
    namespace: str
    revision: int
    status: str


@dataclass(frozen=True)
class UpgradeResult:
    """Outcome of a `helm upgrade --install` invocation.

    `status` is "applied" when helm produced a new revision (first install
    or an actual change to the rendered manifests / values) and "no-change"
    when helm returned 0 without bumping the release revision. The lab
    converge path uses this to skip rollout waits on no-op upgrades.

    Detection is by comparing the release's revision in `helm list -A` before
    and after the upgrade. Helm does not emit a machine-readable "no change"
    marker on stdout, but it *does* hold the revision steady when nothing
    rendered differently -- that's a stable, public contract.

    `revision_before` is None when the release did not exist prior to this
    call (first install). `revision_after` is None only if we could not
    re-list releases after the upgrade (treated as "applied" defensively,
    since we'd rather wait once too often than skip a rollout we needed).
    """

    status: Literal["applied", "no-change"]
    revision_before: int | None
    revision_after: int | None
    output: str


class Helm:
    """Run helm subcommands through a CommandRunner, with per-instance context/timeout."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        version: str | None = None,
        binary: str | Path | None = None,
        verbose: bool = True,
        timeout: float | None = None,
        context: str | None = None,
    ) -> None:
        """Resolve the helm binary (explicit path > mise version > PATH) and set defaults."""
        self.runner = runner or SubprocessRunner()
        self._helm_bin = _resolve(self.runner, version, binary)
        self._context = context
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
        """Run `helm dependency update`, at most once per chart per instance."""
        resolved = chart_path.resolve()
        with self._deps_updated_lock:
            if resolved in self._deps_updated:
                return
            self.runner.run(
                self._with_context([self._helm_bin, "dependency", "update", str(chart_path)]),
                capture=not self.verbose,
                timeout=timeout,
            )
            self._deps_updated.add(resolved)

    def dependency_update_if_stale(
        self, chart_path: Path, *, timeout: float | None = None
    ) -> bool:
        """Run `helm dependency update` only when the lock is stale.

        Cheap mtime gate that elides the (5-15s) subprocess in the common
        re-run case where Chart.lock and charts/ are already up-to-date
        with Chart.yaml. The expensive `helm dependency update` call is the
        single biggest tax on a lab `up` re-run (~18 charts in the install
        plan), so this is a meaningful win for converge-on-rerun.

        Returns True if the update actually ran (or was forced by missing
        artifacts), False if it was skipped because the lock looks fresh.
        The per-instance `_deps_updated` cache is still consulted first so
        a chart only updates once per process even when stale.

        Freshness is decided by `deps_are_fresh` (see its docstring): lock
        and charts/ must exist, the lock must be no older than Chart.yaml,
        and the lock's dependency count must match the artifacts materialized
        under charts/. Any other shape falls through to running the update.
        """
        resolved = chart_path.resolve()
        with self._deps_updated_lock:
            if resolved in self._deps_updated:
                return False
            if deps_are_fresh(resolved):
                # Mark as updated so subsequent calls in this process skip
                # the freshness probe entirely.
                self._deps_updated.add(resolved)
                return False
            self.runner.run(
                self._with_context([self._helm_bin, "dependency", "update", str(chart_path)]),
                capture=not self.verbose,
                timeout=timeout,
            )
            self._deps_updated.add(resolved)
            return True

    def lint(self, chart_path: Path, values: list[Path]) -> None:
        """Run `helm lint` with the given values overlays; raises on lint failure."""
        # See note in upgrade_install on --skip-schema-validation; we use
        # null overrides in values-ci.yaml to wipe inherited map keys past
        # strict subchart schemas.
        args = [self._helm_bin, "lint", str(chart_path), "--skip-schema-validation"]
        args.extend(_values_args(values))
        self.runner.run(self._with_context(args), capture=not self.verbose, timeout=self.timeout)

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
    ) -> UpgradeResult:
        """Run `helm upgrade --install`; classify outcome as applied vs no-change.

        Returns an `UpgradeResult`. The revision-compare classification lets
        converge callers skip rollout waits when helm decided the chart was
        a no-op (same rendered manifests, same values, same chart version).

        Subprocess failures still raise `ExternalCommandError` -- the result
        object is only returned on success.
        """
        revision_before = self._release_revision(release, namespace)
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
        # Always capture stdout so we can surface it on the result object
        # without breaking the existing verbose=True streaming contract for
        # interactive runs: when verbose, we still don't capture so the user
        # sees helm's output live, and the result `output` field is empty.
        result = self.runner.run(
            self._with_context(args),
            capture=not self.verbose,
            timeout=self.timeout,
        )
        revision_after = self._release_revision(release, namespace)
        status: Literal["applied", "no-change"]
        if (
            revision_before is not None
            and revision_after is not None
            and revision_before == revision_after
        ):
            status = "no-change"
        else:
            # Includes first-install (revision_before is None and after is 1)
            # and the can't-re-list defensive case (after is None) -- both
            # surface as "applied" so callers run their normal post-install
            # wait/diagnostics path.
            status = "applied"
        return UpgradeResult(
            status=status,
            revision_before=revision_before,
            revision_after=revision_after,
            output=result.stdout or "",
        )

    def _release_revision(self, release: str, namespace: str) -> int | None:
        """Best-effort revision lookup for a single release.

        Returns the integer revision, or None if the release isn't installed
        or the lookup itself fails. Used to classify upgrade_install outcomes
        as applied vs no-change without coupling the caller to helm's CLI.
        """
        try:
            releases = self.list_releases(all_namespaces=False, namespace=namespace)
        except ExternalCommandError:
            return None
        for info in releases:
            if info.name == release and info.namespace == namespace:
                return info.revision
        return None

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
        """Run a plain `helm upgrade` (no --install, no outcome classification)."""
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
        self.runner.run(self._with_context(args), capture=not self.verbose, timeout=self.timeout)

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
        """Render the chart into `output_dir` via `helm template`; return that dir.

        Local charts with dependencies get a `dependency update` first. On
        render failure, reruns with --debug to capture detail, then raises
        ExternalCommandError (partial output is left in `output_dir`).
        """
        # Resolve to absolute up-front so the path in error messages is
        # actionable from any cwd (engineers need to be able to `ls` it).
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        if is_local_chart(chart_ref) and chart_has_dependencies(Path(chart_ref)):
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
            self._with_context(base_args),
            check=False,
            capture=not self.verbose,
            timeout=self.timeout,
        )
        if result.returncode == 0:
            return output_dir

        debug_args = [*base_args, "--debug"]
        # Always capture the debug rerun's output so we can embed it in the
        # raised error (verbose mode still streams the first attempt above).
        # Bounded by the same timeout as the first attempt: this rerun happens
        # when helm is already misbehaving, which is the last moment to enter
        # an unbounded wait.
        debug_result = self.runner.run(
            self._with_context(debug_args), check=False, capture=True, timeout=self.timeout
        )
        stderr = (debug_result.stderr or result.stderr or "").strip()
        raise ExternalCommandError(
            f"helm template failed for {release} ({chart_ref}); "
            f"rendered (partial) output at: {output_dir}\n{stderr}"
        )

    def test(
        self,
        release: str,
        *,
        namespace: str,
        timeout: str = "10m",
        logs: bool = False,
        subprocess_timeout: float | None = None,
    ) -> CommandResult:
        """Run `helm test <release>`. Returns the CommandResult unconditionally.

        `check=False` so a failed test (rc != 0) returns a result rather than
        raising; the helmrelease test service classifies the verdict from
        stdout/stderr/rc. `logs=True` plumbs `--logs` so helm streams pod
        logs into the result; `subprocess_timeout` is the wall-clock cap
        (falls back to the instance default).
        """
        args = [self._helm_bin, "test", release, "--namespace", namespace, "--timeout", timeout]
        if logs:
            args.append("--logs")
        return self.runner.run(
            self._with_context(args),
            capture=not self.verbose,
            check=False,
            timeout=subprocess_timeout if subprocess_timeout is not None else self.timeout,
        )

    def list_releases(
        self,
        *,
        all_namespaces: bool = True,
        namespace: str | None = None,
    ) -> list[ReleaseInfo]:
        """Return the set of helm releases known to the cluster.

        `all_namespaces=True` (the default) runs `helm list -A`, which is
        what the lab installer needs to dedupe across observability +
        kube-system + cert-manager etc. Pass `all_namespaces=False` together
        with `namespace=` to scope to a single namespace.
        """
        args = [self._helm_bin, "list", "-o", "json"]
        if all_namespaces:
            args.append("-A")
        elif namespace is not None:
            args.extend(["-n", namespace])
        result = self.runner.run(self._with_context(args), capture=True, timeout=self.timeout)
        raw = result.stdout.strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExternalCommandError(
                f"helm list returned non-JSON output: {exc}\n{raw[:200]}"
            ) from exc
        releases: list[ReleaseInfo] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                revision = int(item.get("revision", 0))
            except (TypeError, ValueError):
                revision = 0
            releases.append(
                ReleaseInfo(
                    name=str(item.get("name", "")),
                    namespace=str(item.get("namespace", "")),
                    revision=revision,
                    status=str(item.get("status", "")),
                )
            )
        return releases

    def get_values(self, release: str, *, namespace: str) -> dict[str, Any]:
        """Return the user-supplied values for a release as a dict.

        Runs `helm get values <release> -n <ns> -o json`. Returns an empty
        dict for releases that were installed with no overrides (helm
        emits `null`). Raises ExternalCommandError on subprocess failure
        (release missing, kubeconfig unset) so callers can decide whether
        to swallow or surface the error -- we deliberately do NOT collapse
        "release missing" into an empty dict, since that distinction
        matters for drift detection.
        """
        result = self.runner.run(
            self._with_context([
                self._helm_bin,
                "get",
                "values",
                release,
                "-n",
                namespace,
                "-o",
                "json",
            ]),
            capture=True,
            timeout=self.timeout,
        )
        raw = result.stdout.strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExternalCommandError(
                f"helm get values returned non-JSON output: {exc}\n{raw[:200]}"
            ) from exc
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ExternalCommandError(
                f"helm get values returned non-object JSON for {release}: "
                f"{type(payload).__name__}"
            )
        return payload

    def status(self, release: str, *, namespace: str) -> str:
        """Return combined stdout+stderr of `helm status`; never raises (check=False)."""
        result = self.runner.run(
            self._with_context([self._helm_bin, "status", release, "--namespace", namespace]),
            check=False,
            timeout=self.timeout,
        )
        return result.stdout + result.stderr

    def _with_context(self, args: list[str]) -> list[str]:
        """Append --kube-context when this instance is pinned to one."""
        if self._context is None:
            return args
        return [*args, "--kube-context", self._context]


def _resolve(
    runner: CommandRunner,
    version: str | None,
    binary: str | Path | None,
) -> str:
    """Pick the helm binary: explicit path > mise-managed version > bare `helm`."""
    if binary is not None:
        return str(binary)
    if version is None:
        return "helm"
    return _resolve_via_mise(runner, version)


#: Memo for `mise where helm@<version>`, keyed by the runner that resolved
#: it and then by version. Weak on the runner so an entry dies with the
#: runner it belongs to. The previous `@cache` on a function whose first
#: parameter was the runner was process-global and never evicted, so it held
#: a strong reference to every CommandRunner ever passed -- invisible in a
#: process-per-invocation CLI, an unbounded leak in a long-lived server that
#: builds a container (and a runner) per request.
#:
#: Runner identity stays part of the key: instances sharing one runner share
#: the memo, and one with its own runner pays its own one-shot `mise where`.
_MISE_HELM_PATHS: MutableMapping[CommandRunner, dict[str, str]] = WeakKeyDictionary()
# `@cache` got its atomicity from lru_cache's C-level lock; a plain dict
# needs this to be safe from the validate runner's worker threads.
_MISE_HELM_LOCK = threading.Lock()


def _resolve_via_mise(runner: CommandRunner, version: str) -> str:
    """Locate a pinned helm version via `mise where`; memoized per (runner, version)."""
    with _MISE_HELM_LOCK:
        by_version = _MISE_HELM_PATHS.get(runner)
        if by_version is not None and version in by_version:
            return by_version[version]
    result = runner.run(["mise", "where", f"helm@{version}"], check=True)
    resolved = f"{result.stdout.strip()}/bin/helm"
    with _MISE_HELM_LOCK:
        _MISE_HELM_PATHS.setdefault(runner, {})[version] = resolved
    return resolved


def _clear_mise_cache() -> None:
    """Drop every memoized helm path. Test seam; no production caller."""
    with _MISE_HELM_LOCK:
        _MISE_HELM_PATHS.clear()


def _values_args(values: list[Path]) -> list[str]:
    """Expand values file paths into repeated `--values` CLI args."""
    args: list[str] = []
    for value in values:
        args.extend(["--values", str(value)])
    return args


def _set_args(sets: dict[str, str]) -> list[str]:
    """Expand key/value overrides into repeated `--set key=value` CLI args."""
    args: list[str] = []
    for key, value in sets.items():
        args.extend(["--set", f"{key}={value}"])
    return args
