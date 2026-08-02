"""The built-in validators: their inputs, their execution, and the registry.

One module per validator concern turned out to be three modules and no
concern: `validator_inputs.py` held four path resolvers whose only callers
were the providers here, `validator_registry.py` held a single tuple, and
`phases.schema`/`phases.policy` held the bodies these adapters forwarded to
after an `isinstance` check that transformed nothing. A schema check reached
its subprocess through nine hops, two of which existed only to be crossed.

What is left is the whole chain for one validator, top to bottom: compile the
authored spec into a config, execute the tool against a rendered directory,
fold the result into a category `PhaseResult`. `validators.py` still owns the
contracts (the protocols, the ids, the config shapes) so a third-party
validator has something to implement without importing kubeconform.

`VALIDATOR_REGISTRY` is explicit and in-process: adding a built-in validator
is a reviewed edit to the tuple at the bottom of this file, never dynamic
plugin discovery.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from chart_manager.integrations.kubeconform import Kubeconform, ResourceResult
from chart_manager.integrations.kyverno import Kyverno, PolicyResult
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from chart_manager.services.manifest_validation.models import PhaseResult
from chart_manager.services.manifest_validation.paths import has_manifests, require_within
from chart_manager.services.manifest_validation.validators import (
    KubeconformConfig,
    KyvernoConfig,
    ManifestValidator,
    ValidatorCategory,
    ValidatorCompileContext,
    ValidatorConfig,
    ValidatorId,
    ValidatorInvocation,
    ValidatorProvider,
    validate_registry,
)

# --- input resolution ------------------------------------------------------


def discover_policy_paths(repo_root: Path, chart_path: Path) -> tuple[Path, ...]:
    """Return existing repository-wide and per-chart policy directories."""
    return tuple(
        candidate.resolve()
        for candidate in (repo_root / "policies", chart_path / "policies")
        if candidate.is_dir()
    )


def resolve_policy_paths(
    *,
    repo_root: Path,
    chart_path: Path,
    spec_path: Path,
    extras: list[str],
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Resolve discovered and authored chart-relative policy directories."""
    policies = list(discover_policy_paths(repo_root, chart_path))
    warnings: list[str] = []
    for extra in extras:
        selected = (chart_path / extra).resolve()
        if selected.is_dir():
            require_within(
                selected,
                chart_path,
                label=f"{spec_path}: chart-relative policy directory {extra!r}",
            )
        elif selected.exists():
            warnings.append(f"{spec_path}: policy path is not a directory: {selected}")
            continue
        else:
            warnings.append(f"{spec_path}: policy directory does not exist: {selected}")
            continue
        if selected not in policies:
            policies.append(selected)
    return tuple(policies), tuple(warnings)


def resolve_schema_locations(
    locations: list[str],
    *,
    repo_root: Path,
    spec_path: Path,
) -> tuple[str, ...]:
    """Keep kubeconform keywords/URLs and validate local schema templates."""
    return tuple(
        _resolve_schema_location(location, repo_root, spec_path=spec_path)
        for location in locations
    )


def _resolve_schema_location(
    location: str,
    repo_root: Path,
    *,
    spec_path: Path,
) -> str:
    if location == "default":
        return location
    parsed = urlsplit(location)
    if parsed.scheme:
        return location
    if not location.strip():
        raise SpecError(f"{spec_path}: schema location must not be empty")

    resolved = (repo_root / location).resolve()
    label = f"{spec_path}: local schema location {location!r}"
    require_within(resolved, repo_root, label=label)

    template_start = location.find("{{")
    if template_start < 0:
        if not resolved.exists():
            raise SpecError(f"{label} does not exist: {resolved}")
        if not (resolved.is_file() or resolved.is_dir()):
            raise SpecError(f"{label} is not a regular file or directory: {resolved}")
        return str(resolved)

    static_prefix = location[:template_start]
    prefix_path = Path(static_prefix)
    anchor_relative = (
        prefix_path if static_prefix.endswith(("/", "\\")) else prefix_path.parent
    )
    anchor = (repo_root / anchor_relative).resolve()
    require_within(anchor, repo_root, label=label)
    if not anchor.exists():
        raise SpecError(f"{label} has a missing template base directory: {anchor}")
    if not anchor.is_dir():
        raise SpecError(f"{label} template base is not a directory: {anchor}")
    return str(resolved)


# --- execution -------------------------------------------------------------


class KubeconformValidator:
    """Execute kubeconform as a schema-category validator.

    Empty rendered_dir -> SKIP. Tool crash -> FAIL with error_type="tool"
    (`Outcome.TOOL`), because the underlying issue is kubeconform breaking,
    not a chart-author problem. Schema violations -> FAIL with a
    human-scannable one-line-per-finding detail block.
    """

    def __init__(self, integration: Kubeconform) -> None:
        """Bind one kubeconform adapter; probing the binary is the caller's job."""
        self.integration = integration

    def validate(
        self,
        rendered_dir: Path,
        config: ValidatorConfig,
    ) -> PhaseResult:
        """Run kubeconform over one row's rendered manifests."""
        if not isinstance(config, KubeconformConfig):
            raise TypeError("kubeconform received incompatible compiled config")
        if not rendered_dir.exists() or not has_manifests(rendered_dir):
            return PhaseResult(phase="schema", status="SKIP", detail="no manifests")

        try:
            report = self.integration.validate(
                rendered_dir,
                kubernetes_version=config.kubernetes_version,
                schema_locations=list(config.schema_locations) or None,
            )
        except ExternalCommandError as exc:
            return PhaseResult(
                phase="schema",
                status="FAIL",
                detail=str(exc),
                error_type="tool",
            )

        if not report.has_failures():
            return PhaseResult(phase="schema", status="PASS")

        return PhaseResult(
            phase="schema",
            status="FAIL",
            detail=_format_schema_findings(report.invalid()),
        )


class KyvernoValidator:
    """Execute Kyverno as a policy-category validator.

    Empty policy_paths -> SKIP("no policies discovered") so charts without
    any policy coverage surface visibly (the run summary tallies these).
    Empty rendered_dir -> SKIP("no manifests"). Tool crash -> FAIL with
    error_type="tool" (`Outcome.TOOL`). Policy violations -> FAIL with one
    line per finding.
    """

    def __init__(self, integration: Kyverno) -> None:
        """Bind one Kyverno adapter; probing the binary is the caller's job."""
        self.integration = integration

    def validate(
        self,
        rendered_dir: Path,
        config: ValidatorConfig,
    ) -> PhaseResult:
        """Run kyverno over one row's rendered manifests."""
        if not isinstance(config, KyvernoConfig):
            raise TypeError("kyverno received incompatible compiled config")
        if not config.policy_paths:
            return PhaseResult(
                phase="policy", status="SKIP", detail="no policies discovered"
            )
        if not rendered_dir.exists() or not has_manifests(rendered_dir):
            return PhaseResult(phase="policy", status="SKIP", detail="no manifests")

        try:
            report = self.integration.apply(
                rendered_dir, policy_paths=list(config.policy_paths)
            )
        except ExternalCommandError as exc:
            return PhaseResult(
                phase="policy",
                status="FAIL",
                detail=str(exc),
                error_type="tool",
            )

        warns = report.warnings()
        if not report.has_failures():
            # Surface warns as an advisory on a PASS row (no exit-code change);
            # rendering picks them up via the non-empty detail on PASS phases.
            if warns:
                return PhaseResult(
                    phase="policy",
                    status="PASS",
                    detail="warnings:\n" + _format_policy_findings(warns),
                )
            return PhaseResult(phase="policy", status="PASS")

        detail = _format_policy_findings(report.failures())
        if warns:
            detail += "\n\nwarnings:\n" + _format_policy_findings(warns)
        return PhaseResult(
            phase="policy",
            status="FAIL",
            detail=detail,
        )


def _format_schema_findings(resources: tuple[ResourceResult, ...]) -> str:
    """Render kubeconform findings as one `kind/name (file): msg` line each."""
    lines: list[str] = []
    for r in resources:
        msg = r.msg or ""
        lines.append(f"{r.kind}/{r.name} ({r.filename}): {msg}".rstrip(": "))
    return "\n".join(lines)


def _format_policy_findings(findings: tuple[PolicyResult, ...]) -> str:
    """Render kyverno findings as one `policy/rule: kind/name: msg` line each."""
    lines: list[str] = []
    for f in findings:
        msg = f.message or ""
        lines.append(
            f"{f.policy}/{f.rule}: {f.resource_kind}/{f.resource_name}: {msg}".rstrip(": ")
        )
    return "\n".join(lines)


# --- providers -------------------------------------------------------------


class KubeconformProvider:
    """Own kubeconform compilation, identity, and construction."""

    validator_id: str = ValidatorId.KUBECONFORM
    category: ValidatorCategory = ValidatorCategory.SCHEMA
    order: int = 100

    def compile(self, context: ValidatorCompileContext) -> ValidatorInvocation:
        """Resolve authored schema locations into a kubeconform config."""
        spec = context.spec
        locations = (
            resolve_schema_locations(
                spec.schema_locations,
                repo_root=context.repo_root,
                spec_path=context.spec_path,
            )
            if spec.validators.kubeconform
            else ()
        )
        return ValidatorInvocation(
            validator_id=self.validator_id,
            category=self.category,
            order=self.order,
            enabled=spec.validators.kubeconform,
            config=KubeconformConfig(
                kubernetes_version=spec.kubernetes_version,
                schema_locations=locations,
            ),
        )

    def build(
        self,
        *,
        command_runner: CommandRunner,
        timeout: float | None,
    ) -> ManifestValidator:
        """Build the kubeconform executor without probing its binary."""
        return KubeconformValidator(
            Kubeconform(runner=command_runner, timeout=timeout)
        )


class KyvernoProvider:
    """Own Kyverno compilation, identity, and construction."""

    validator_id: str = ValidatorId.KYVERNO
    category: ValidatorCategory = ValidatorCategory.POLICY
    order: int = 200

    def compile(self, context: ValidatorCompileContext) -> ValidatorInvocation:
        """Resolve discovered and authored policy directories into a config."""
        paths, warnings = (
            resolve_policy_paths(
                repo_root=context.repo_root,
                chart_path=context.chart_path,
                spec_path=context.spec_path,
                extras=context.spec.policies.extra,
            )
            if context.spec.validators.policy
            else ((), ())
        )
        return ValidatorInvocation(
            validator_id=self.validator_id,
            category=self.category,
            order=self.order,
            enabled=context.spec.validators.policy,
            config=KyvernoConfig(policy_paths=paths),
            warnings=warnings,
        )

    def build(
        self,
        *,
        command_runner: CommandRunner,
        timeout: float | None,
    ) -> ManifestValidator:
        """Build the Kyverno executor without probing its binary."""
        return KyvernoValidator(Kyverno(runner=command_runner, timeout=timeout))


_PROVIDERS: tuple[ValidatorProvider, ...] = (
    KubeconformProvider(),
    KyvernoProvider(),
)
VALIDATOR_REGISTRY = validate_registry(_PROVIDERS)
