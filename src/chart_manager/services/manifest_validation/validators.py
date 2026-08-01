"""Closed validator registry and validator-neutral execution contracts.

Validator *identity* (the concrete tool) is deliberately separate from
validator *category* (the stable schema/policy quality gate exposed by the
CLI and wire formats).  The registry is explicit and in-process: adding a
built-in validator is a reviewed code change, never dynamic plugin discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from chart_manager.api.lifecycle.v1alpha1 import ManifestValidationSpec
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.services.manifest_validation.models import PhaseResult


class ValidatorId(StrEnum):
    """Stable identity of a concrete manifest validator."""

    KUBECONFORM = "kubeconform"
    KYVERNO = "kyverno"


class ValidatorCategory(StrEnum):
    """Stable user-facing quality gates."""

    SCHEMA = "schema"
    POLICY = "policy"


@dataclass(frozen=True)
class ValidatorCompileContext:
    """Resolved chart inputs available to every provider compiler."""

    spec: ManifestValidationSpec
    repo_root: Path
    chart_path: Path
    spec_path: Path


def validate_registry(
    providers: tuple[ValidatorProvider, ...],
) -> tuple[ValidatorProvider, ...]:
    """Validate and deterministically order a closed registry."""

    ids: set[str] = set()
    orders: set[int] = set()
    for provider in providers:
        if not provider.validator_id:
            raise ValueError("validator id must not be empty")
        if provider.validator_id in ids:
            raise ValueError(f"duplicate validator id: {provider.validator_id}")
        if provider.order in orders:
            raise ValueError(f"duplicate validator order: {provider.order}")
        ids.add(provider.validator_id)
        orders.add(provider.order)
    return tuple(sorted(providers, key=lambda provider: provider.order))


@dataclass(frozen=True)
class KubeconformConfig:
    """Resolved kubeconform inputs."""

    kubernetes_version: str | None
    schema_locations: tuple[str, ...]


@dataclass(frozen=True)
class KyvernoConfig:
    """Resolved Kyverno inputs."""

    policy_paths: tuple[Path, ...]


ValidatorConfig = KubeconformConfig | KyvernoConfig | object


@dataclass(frozen=True)
class ValidatorInvocation:
    """One compiled, ordered validator invocation for a chart/environment."""

    validator_id: str
    category: ValidatorCategory
    order: int
    enabled: bool
    config: ValidatorConfig
    warnings: tuple[str, ...] = ()


class ManifestValidator(Protocol):
    """Concrete validator adapter consumed by the generic runner."""

    def validate(
        self,
        rendered_dir: Path,
        config: ValidatorConfig,
    ) -> PhaseResult:
        """Validate rendered manifests and return a category result."""


class ValidatorProvider(Protocol):
    """One cohesive built-in extension point from spec to execution."""

    validator_id: str
    category: ValidatorCategory
    order: int

    def compile(self, context: ValidatorCompileContext) -> ValidatorInvocation:
        """Compile authored and resolved inputs for this validator."""

    def build(
        self,
        *,
        command_runner: CommandRunner,
        timeout: float | None,
    ) -> ManifestValidator:
        """Build this validator's executor without probing its binary."""
