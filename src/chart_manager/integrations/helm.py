"""Thin wrapper around the `helm` CLI: install/upgrade, template, lint, test, list."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.duration import format_duration
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.preflight import Check, probe_binary


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


@dataclass(frozen=True)
class PackageResult:
    """A packaged chart archive prepared for an OCI push."""

    path: Path
    output: str


@dataclass(frozen=True)
class PushResult:
    """Machine-useful identity returned by ``helm push``."""

    reference: str
    digest: str | None
    output: str


class Helm:
    """Run helm subcommands through a CommandRunner, pinned to one binary, cluster and cap.

    `context=None` addresses the ambient kubeconfig context; `timeout=None`
    leaves each call unbounded. `verbose` streams helm's output to the
    terminal; parallel callers pass False so concurrent runs don't interleave.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        binary: str,
        verbose: bool = True,
        timeout: float | None,
        context: str | None,
    ) -> None:
        """Bind a runner and pin every invocation to a binary, context and timeout."""
        self.runner = runner
        self._helm_bin = binary
        self._context = context
        self.verbose = verbose
        self.timeout = timeout

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = False,
        timeout: float | None = None,
        pinned: bool = True,
    ) -> CommandResult:
        """Every helm invocation: `<helm> <args>`, pinned to the kube-context and cap.

        Output streams when verbose unless `capture` asks for it. `timeout`
        overrides the instance cap. `pinned=False` drops `--kube-context` for
        package and push, which touch no cluster.
        """
        argv = [self._helm_bin, *args]
        if pinned and self._context is not None:
            argv += ["--kube-context", self._context]
        return self.runner.run(
            argv,
            check=check,
            capture=capture or not self.verbose,
            timeout=timeout if timeout is not None else self.timeout,
        )

    def preflight(self) -> tuple[Check, ...]:
        """Report whether the helm this instance resolved is usable.

        Probes `self._helm_bin`, not the literal string "helm": the caller
        may have pinned a mise-managed binary, and a preflight that checked a
        different helm than the one every other method runs would be worse
        than no preflight.
        """
        return (
            probe_binary(
                self.runner,
                self._helm_bin,
                name="helm",
                version_args=("version", "--short"),
                remediation="install helm 3.x -- https://helm.sh/docs/intro/install/",
            ),
        )

    def dependency_update(self, chart_path: Path, *, timeout: float) -> None:
        """Run `helm dependency update` for a local chart, killed after `timeout` seconds."""
        self._run(["dependency", "update", str(chart_path)], timeout=timeout)

    def package(
        self,
        chart_path: Path,
        output_dir: Path,
        *,
        version: str | None = None,
    ) -> PackageResult:
        """Package a chart without editing its source metadata."""
        output_dir.mkdir(parents=True, exist_ok=True)
        args = ["package", str(chart_path), "--destination", str(output_dir)]
        if version is not None:
            args.extend(["--version", version])
        result = self._run(args, capture=True, pinned=False)
        marker = "Successfully packaged chart and saved it to:"
        archive_line = next(
            (line for line in result.stdout.splitlines() if marker in line),
            None,
        )
        if archive_line is None:
            raise ExternalCommandError(
                "helm package succeeded but did not report the package archive path"
            )
        archive = Path(archive_line.split(marker, 1)[1].strip())
        if not archive.is_absolute():
            archive = output_dir / archive.name
        return PackageResult(path=archive, output=result.stdout)

    def push(
        self,
        package_path: Path,
        repository: str,
        *,
        ca_file: Path | None = None,
        expected_reference: str | None = None,
    ) -> PushResult:
        """Push one package to an OCI repository and retain its ref/digest.

        A zero subprocess exit is authoritative for the remote mutation.
        Helm's human-readable success lines have moved between output streams
        and gained presentation decoration across versions, so callers that
        know the packaged chart identity provide ``expected_reference`` as a
        retry-safe fallback rather than turning an already-completed push into
        a false failure.
        """
        if not repository.startswith("oci://"):
            raise ValueError("OCI repository must start with oci://")
        args = ["push", str(package_path), repository.rstrip("/")]
        if ca_file is not None:
            args.extend(["--ca-file", str(ca_file)])
        result = self._run(args, capture=True, pinned=False)
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        pushed = _helm_output_value(output, "Pushed")
        digest = _helm_output_value(output, "Digest")
        if pushed is None:
            if expected_reference is None:
                raise ExternalCommandError(
                    "helm push succeeded but did not report the pushed OCI reference"
                )
            reference = expected_reference
        else:
            reference = pushed if pushed.startswith("oci://") else f"oci://{pushed}"
        return PushResult(reference=reference, digest=digest, output=output)

    def lint(self, chart_path: Path, values: list[Path]) -> None:
        """Run `helm lint` with the given values overlays; raises on lint failure."""
        # See note in upgrade_install on --skip-schema-validation; we use
        # null overrides in values-ci.yaml to wipe inherited map keys past
        # strict subchart schemas.
        self._run(["lint", str(chart_path), "--skip-schema-validation", *_values_args(values)])

    def upgrade_install(
        self,
        release: str,
        chart_ref: str | Path,
        *,
        namespace: str,
        values: list[Path] | None = None,
        sets: dict[str, str] | None = None,
        timeout: float,
        wait: bool = True,
        version: str | None = None,
        repo: str | None = None,
    ) -> UpgradeResult:
        """Run `helm upgrade --install` within `timeout` seconds; classify the outcome.

        Returns an `UpgradeResult`. The revision-compare classification lets
        converge callers skip rollout waits when helm decided the chart was
        a no-op (same rendered manifests, same values, same chart version).

        Subprocess failures still raise `ExternalCommandError` -- the result
        object is only returned on success.
        """
        revision_before = self._release_revision(release, namespace)
        args = [
            "upgrade",
            "--install",
            release,
            str(chart_ref),
            "--namespace",
            namespace,
            "--create-namespace",
            "--timeout",
            format_duration(timeout),
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
        if version is not None:
            args.extend(["--version", version])
        if repo is not None:
            args.extend(["--repo", repo])
        args.extend(_values_args(values or []))
        args.extend(_set_args(sets or {}))
        # When verbose, helm's output streams live and `output` is empty.
        result = self._run(args)
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
        include_crds: bool = False,
    ) -> Path:
        """Render the chart into `output_dir` via `helm template`; return that dir.

        On render failure, reruns with --debug to capture detail, then raises
        ExternalCommandError (partial output is left in `output_dir`).
        """
        # Resolve to absolute up-front so the path in error messages is
        # actionable from any cwd (engineers need to be able to `ls` it).
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        base_args = [
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
        if include_crds:
            base_args.append("--include-crds")

        # Deliberately NOT passing --skip-schema-validation: at template time
        # we want subchart schema errors to surface as render failures rather
        # than be silently masked. lint/upgrade_install skip them for
        # documented istio/values-overlay reasons that don't apply here.
        result = self._run(base_args, check=False)
        if result.returncode == 0:
            return output_dir

        debug_args = [*base_args, "--debug"]
        # Always capture the debug rerun's output so we can embed it in the
        # raised error (verbose mode still streams the first attempt above).
        # Bounded by the same timeout as the first attempt: this rerun happens
        # when helm is already misbehaving, which is the last moment to enter
        # an unbounded wait.
        debug_result = self._run(debug_args, check=False, capture=True)
        stderr = (debug_result.stderr or result.stderr or "").strip()
        raise ExternalCommandError(
            f"helm template failed for {release} ({chart_ref}); "
            f"rendered (partial) output at: {output_dir}\n{stderr}",
            stderr=stderr,
            returncode=result.returncode,
        )

    def test(
        self,
        release: str,
        *,
        namespace: str,
        timeout: float,
        logs: bool = False,
        subprocess_timeout: float | None,
    ) -> CommandResult:
        """Run `helm test <release>`, waiting up to `timeout` seconds for its hooks.

        Returns the CommandResult whatever the exit code: the callers judge
        the verdict. `logs=True` adds pod logs to the output;
        `subprocess_timeout` caps the subprocess (None uses the instance cap).
        """
        args = ["test", release, "--namespace", namespace, "--timeout", format_duration(timeout)]
        if logs:
            args.append("--logs")
        return self._run(args, check=False, timeout=subprocess_timeout)

    def list_releases(
        self,
        *,
        all_namespaces: bool = True,
        namespace: str | None = None,
        any_status: bool = False,
    ) -> list[ReleaseInfo]:
        """Return the set of helm releases known to the cluster.

        `all_namespaces=True` (the default) runs `helm list -A`, which is
        what the lab installer needs to dedupe across observability +
        kube-system + cert-manager etc. Pass `all_namespaces=False` together
        with `namespace=` to scope to a single namespace. Helm lists only deployed and
        failed releases unless `any_status` adds `--all`.
        """
        args = ["list", "-o", "json"]
        if all_namespaces:
            args.append("-A")
        elif namespace is not None:
            args.extend(["-n", namespace])
        if any_status:
            args.append("--all")
        raw = self._run(args, capture=True).stdout.strip()
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

    def manifest(self, release: str, *, namespace: str) -> str:
        """Return the rendered manifest of an installed release (`helm get manifest`)."""
        args = ["get", "manifest", release, "--namespace", namespace]
        return self._run(args, capture=True).stdout


def _helm_output_value(output: str, label: str) -> str | None:
    """Read a presentation-tolerant ``Label: value`` emitted by Helm."""
    prefix = f"{label}:"
    for line in output.splitlines():
        clean = _ANSI_ESCAPE.sub("", line).strip()
        if clean.startswith(prefix):
            value = clean.removeprefix(prefix).strip()
            return value or None
    return None


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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
