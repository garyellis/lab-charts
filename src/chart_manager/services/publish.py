"""Headless, batch-safe Helm chart publishing."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chart_manager.integrations.helm import Helm, PackageResult
from chart_manager.plumbing.errors import ChartManagerError, SpecError
from chart_manager.services.domain.charts import ChartRepository
from chart_manager.settings import DEFAULT_CHARTS_DIR

_SEMVER = re.compile(
    r"^(?P<core>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SUFFIX_IDENTIFIER = r"[0-9A-Za-z](?:[0-9A-Za-z-]*[0-9A-Za-z])?"
_SUFFIX = re.compile(rf"^{_SUFFIX_IDENTIFIER}(?:\.{_SUFFIX_IDENTIFIER})*$")


@dataclass(frozen=True)
class PublishedChart:
    """Result for one chart after the push phase."""

    chart: str
    version: str
    reference: str | None = None
    digest: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class PublishResult:
    """Consolidated batch outcome."""

    charts: tuple[PublishedChart, ...]

    @property
    def ok(self) -> bool:
        return all(chart.ok for chart in self.charts)


class PublishService:
    """Prepare every chart before allowing any remote mutation."""

    def __init__(
        self,
        root: Path,
        *,
        helm: Helm,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
    ) -> None:
        self.repository = ChartRepository(root, charts_dir=charts_dir)
        self.helm = helm

    def publish(
        self,
        charts: list[str] | tuple[str, ...],
        *,
        repository: str,
        version_suffix: str | None = None,
        version: str | None = None,
        ca_file: Path | None = None,
    ) -> PublishResult:
        """Package the whole batch, then push each prepared archive.

        Preparation failures raise and therefore push nothing. Pushes cannot
        be rolled back, so remote failures are consolidated after attempting
        every prepared chart.
        """
        selected = tuple(dict.fromkeys(charts))
        if not selected:
            raise SpecError("at least one chart name is required")
        if not repository.startswith("oci://"):
            raise SpecError("repository must be an OCI URL beginning with oci://")
        if version is not None and version_suffix is not None:
            raise SpecError("--version and --version-suffix are mutually exclusive")
        if version is not None and len(selected) != 1:
            raise SpecError("--version is only valid when publishing exactly one chart")
        if version is not None:
            validate_semver(version, label="version")

        with tempfile.TemporaryDirectory(prefix="chart-manager-publish-") as work:
            output_dir = Path(work)
            prepared: list[tuple[str, str, PackageResult]] = []
            for name in selected:
                chart = self.repository.get(name)
                base_version = chart.metadata.version
                if base_version is None:
                    raise SpecError(f"chart '{name}' has no version in Chart.yaml")
                target_version = version or (
                    with_version_suffix(base_version, version_suffix)
                    if version_suffix is not None
                    else validate_semver(base_version, label=f"chart '{name}' version")
                )
                self.helm.dependency_update(chart.path)
                package = self.helm.package(
                    chart.path,
                    output_dir,
                    version=target_version if target_version != base_version else None,
                )
                prepared.append((name, target_version, package))

            outcomes: list[PublishedChart] = []
            for name, target_version, package in prepared:
                try:
                    pushed = self.helm.push(
                        package.path,
                        repository,
                        ca_file=ca_file,
                    )
                except ChartManagerError as exc:
                    outcomes.append(
                        PublishedChart(
                            chart=name,
                            version=target_version,
                            error=str(exc),
                        )
                    )
                else:
                    outcomes.append(
                        PublishedChart(
                            chart=name,
                            version=target_version,
                            reference=pushed.reference,
                            digest=pushed.digest,
                        )
                    )
            return PublishResult(tuple(outcomes))


def validate_semver(version: str, *, label: str = "version") -> str:
    """Validate strict SemVer 2.0, including numeric identifier rules."""
    match = _SEMVER.fullmatch(version)
    if match is None or _has_leading_zero_numeric(match.group("pre")):
        raise SpecError(f"invalid SemVer {label}: {version!r}")
    return version


def with_version_suffix(base: str, suffix: str) -> str:
    """Append prerelease identifiers while preserving existing metadata."""
    match = _SEMVER.fullmatch(base)
    if match is None or _has_leading_zero_numeric(match.group("pre")):
        raise SpecError(f"invalid SemVer chart version: {base!r}")
    if not suffix or _SUFFIX.fullmatch(suffix) is None or _has_leading_zero_numeric(suffix):
        raise SpecError(f"invalid SemVer prerelease suffix: {suffix!r}")
    core = f"{match.group('core')}.{match.group('minor')}.{match.group('patch')}"
    prerelease = ".".join(part for part in (match.group("pre"), suffix) if part)
    build = f"+{match.group('build')}" if match.group("build") else ""
    return f"{core}-{prerelease}{build}"


def _has_leading_zero_numeric(value: str | None) -> bool:
    return bool(
        value
        and any(
            part.isdigit() and len(part) > 1 and part.startswith("0")
            for part in value.split(".")
        )
    )


__all__ = [
    "PublishResult",
    "PublishService",
    "PublishedChart",
    "validate_semver",
    "with_version_suffix",
]
