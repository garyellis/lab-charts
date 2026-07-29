"""Adapters from concrete integrations to the validator-neutral contract."""

from __future__ import annotations

import json
from pathlib import Path

from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.services.manifest_validation import phases
from chart_manager.services.manifest_validation.models import PhaseResult
from chart_manager.services.manifest_validation.validator_inputs import (
    resolve_policy_paths,
    resolve_schema_locations,
)
from chart_manager.services.manifest_validation.validators import (
    KubeconformConfig,
    KyvernoConfig,
    ManifestValidator,
    ValidationContext,
    ValidatorCategory,
    ValidatorCompileContext,
    ValidatorConfig,
    ValidatorId,
    ValidatorInvocation,
)


class KubeconformValidator:
    """Execute kubeconform as a schema-category validator."""

    def __init__(self, integration: Kubeconform) -> None:
        self.integration = integration

    def validate(
        self,
        context: ValidationContext,
        config: ValidatorConfig,
    ) -> PhaseResult:
        if not isinstance(config, KubeconformConfig):
            raise TypeError("kubeconform received incompatible compiled config")
        return phases.schema(
            context.row,
            kubeconform=self.integration,
            rendered_dir=context.rendered_dir,
            kubernetes_version=config.kubernetes_version,
            schema_locations=list(config.schema_locations) or None,
        )


class KyvernoValidator:
    """Execute Kyverno as a policy-category validator."""

    def __init__(self, integration: Kyverno) -> None:
        self.integration = integration

    def validate(
        self,
        context: ValidationContext,
        config: ValidatorConfig,
    ) -> PhaseResult:
        if not isinstance(config, KyvernoConfig):
            raise TypeError("kyverno received incompatible compiled config")
        return phases.policy(
            context.row,
            kyverno=self.integration,
            rendered_dir=context.rendered_dir,
            policy_paths=list(config.policy_paths),
        )


class KubeconformProvider:
    """Own kubeconform compilation, lifecycle identity, and construction."""

    validator_id: str = ValidatorId.KUBECONFORM
    category: ValidatorCategory = ValidatorCategory.SCHEMA
    order: int = 100
    lifecycle_action_kind: str = "schema-validate"

    def compile(self, context: ValidatorCompileContext) -> ValidatorInvocation:
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
            lifecycle_action_kind=self.lifecycle_action_kind,
            enabled=spec.validators.kubeconform,
            config=KubeconformConfig(
                kubernetes_version=spec.kubernetes_version,
                schema_locations=locations,
            ),
            lifecycle_metadata=(
                ("kubernetesVersion", spec.kubernetes_version or ""),
                ("schemaLocations", json.dumps(locations)),
            ),
            lifecycle_additional_paths=tuple(
                Path(location)
                for location in locations
                if Path(location).is_absolute()
            ),
        )

    def build(
        self,
        *,
        command_runner: CommandRunner,
        timeout: float | None,
    ) -> ManifestValidator:
        return KubeconformValidator(
            Kubeconform(runner=command_runner, timeout=timeout)
        )


class KyvernoProvider:
    """Own Kyverno compilation, lifecycle identity, and construction."""

    validator_id: str = ValidatorId.KYVERNO
    category: ValidatorCategory = ValidatorCategory.POLICY
    order: int = 200
    lifecycle_action_kind: str = "policy-validate"

    def compile(self, context: ValidatorCompileContext) -> ValidatorInvocation:
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
            lifecycle_action_kind=self.lifecycle_action_kind,
            enabled=context.spec.validators.policy,
            config=KyvernoConfig(policy_paths=paths),
            lifecycle_metadata=(
                ("policyPaths", json.dumps([str(path) for path in paths])),
            ),
            lifecycle_additional_paths=paths,
            warnings=warnings,
        )

    def build(
        self,
        *,
        command_runner: CommandRunner,
        timeout: float | None,
    ) -> ManifestValidator:
        return KyvernoValidator(Kyverno(runner=command_runner, timeout=timeout))
