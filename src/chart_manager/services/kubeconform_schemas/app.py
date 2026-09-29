"""Repository-level orchestration for eager schema synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from chart_manager.domain.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from chart_manager.services.kubeconform_schemas.crd import generate_crd_schemas
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaIntegrityError,
)
from chart_manager.services.kubeconform_schemas.inventory import (
    SchemaInventory,
    scan_rendered_directory,
)
from chart_manager.services.kubeconform_schemas.models import (
    AuthoredSchemaPolicy,
    MaterializedSchema,
    SchemaRequirement,
    SchemaScope,
)
from chart_manager.services.kubeconform_schemas.source import KubeconformSchemaSource
from chart_manager.services.kubeconform_schemas.store import KubeconformSchemaStore
from chart_manager.services.kubeconform_schemas.sync import (
    KubeconformSchemaSyncRequest,
    KubeconformSchemaSyncResult,
    KubeconformSchemaSyncService,
)
from chart_manager.services.manifest_validation.app import ManifestValidationService
from chart_manager.services.manifest_validation.catalog import build_catalog
from chart_manager.services.manifest_validation.models import (
    ManifestValidationTarget,
    RunRequest,
)


@dataclass(frozen=True)
class RepositoryKubeconformSchemaSyncResult:
    """One complete repository inventory and its published store generation."""

    sync: KubeconformSchemaSyncResult
    rows: int
    required: int
    generated: int
    local: int


class RepositoryKubeconformSchemaService:
    """Render every validation row and publish one complete schema generation."""

    def __init__(
        self,
        *,
        workspace: RepositoryWorkspace,
        validation: ManifestValidationService,
        sync: KubeconformSchemaSyncService,
    ) -> None:
        self.workspace = workspace
        self.validation = validation
        self.sync_service = sync

    def sync(
        self,
        *,
        update: bool = False,
        offline: bool = False,
        workers: int = 0,
    ) -> RepositoryKubeconformSchemaSyncResult:
        """Build the complete inventory before advancing any active generation."""
        policy = self.workspace.validation
        if policy is None:
            raise KubeconformSchemaConfigurationError(
                f"{self.workspace.marker} has no spec.validation; configure schema policy "
                "before running `chart-manager schemas sync --update`"
            )
        workspace_name = _require_workspace_name(self.workspace)

        catalog = build_catalog(
            self.workspace.root,
            charts_dir=self.workspace.charts_dir,
        )
        if catalog.errors:
            raise KubeconformSchemaConfigurationError(
                "cannot build schema inventory: " + "; ".join(catalog.errors)
            )
        targets = catalog.by_name()
        if not targets:
            raise KubeconformSchemaConfigurationError(
                "no enabled chart validation targets were found"
            )

        with TemporaryDirectory(prefix="chart-manager-schema-render-") as temporary:
            render_root = Path(temporary)
            outcome = self.validation.run(
                RunRequest(
                    root=self.workspace.root,
                    skip_change_detection=True,
                    phases=frozenset({"render"}),
                    out=render_root,
                    keep=True,
                    workers=workers,
                    offline=offline,
                )
            )
            failures = [
                f"{row.row.chart}/{row.row.env}: {row.phases['render'].detail or 'render failed'}"
                for row in outcome.result.rows
                if row.phases["render"].status != "PASS"
            ]
            if outcome.result.spec_errors:
                failures.extend(outcome.result.spec_errors)
            if failures:
                raise KubeconformSchemaIntegrityError(
                    "schema inventory render failed: " + "; ".join(failures)
                )

            inventories: list[SchemaInventory] = []
            for row in outcome.result.rows:
                target = targets.get(row.row.chart)
                if target is None:
                    raise KubeconformSchemaIntegrityError(
                        f"rendered schema row has no catalog target: {row.row.chart}"
                    )
                inventories.append(
                    scan_rendered_directory(
                        render_root / row.row.chart / row.row.env,
                        scope=SchemaScope(
                            chart=row.row.chart,
                            environment=row.row.env,
                        ),
                        allow_missing=frozenset(target.spec.ignore_missing_schemas),
                    )
                )

            inventory = SchemaInventory.merge(*inventories)
            generated = (
                generate_crd_schemas(inventory.crds) if policy.schemas.generate_from_crds else ()
            )
            local = _materialize_local_schemas(
                self.workspace.root,
                inventory.requirements,
                targets,
            )
            request = KubeconformSchemaSyncRequest(
                workspace=workspace_name,
                policy=AuthoredSchemaPolicy(
                    kubernetes_version=policy.kubernetes_version,
                    generate_from_crds=policy.schemas.generate_from_crds,
                    catalog_repository=policy.schemas.catalog.repository,
                    catalog_track=policy.schemas.catalog.track,
                ),
                requirements=inventory.requirements,
                materialized=(*generated, *local),
                lock_path=self.workspace.root / SCHEMA_LOCK_FILE,
                update=update,
                offline=offline,
            )
            synchronized = self.sync_service.sync(request)

        return RepositoryKubeconformSchemaSyncResult(
            sync=synchronized,
            rows=len(outcome.result.rows),
            required=len(inventory.requirements),
            generated=len(generated),
            local=len(local),
        )


def _materialize_local_schemas(
    root: Path,
    requirements: tuple[SchemaRequirement, ...],
    targets: dict[str, ManifestValidationTarget],
) -> tuple[MaterializedSchema, ...]:
    """Read lifecycle-local templates only for GVKs that actually require them."""
    materialized: dict[tuple[str, str, str, str, str], MaterializedSchema] = {}
    for requirement in requirements:
        target = targets[requirement.scope.chart]
        locations = target.spec.schema_locations
        for template in locations:
            relative = _expand_schema_template(template, requirement)
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                continue
            artifact = MaterializedSchema(
                gvk=requirement.gvk,
                source="local",
                scope=requirement.scope,
                content=path.read_bytes(),
                source_reference=path.relative_to(root.resolve()).as_posix(),
            )
            key = (
                requirement.scope.chart,
                requirement.scope.environment or "",
                requirement.gvk.group,
                requirement.gvk.version,
                requirement.gvk.kind,
            )
            previous = materialized.get(key)
            if previous is not None and previous.content != artifact.content:
                raise KubeconformSchemaIntegrityError(
                    f"conflicting local schemas for {requirement.gvk.key} "
                    f"in {requirement.scope.key}"
                )
            materialized[key] = artifact
            break
    return tuple(materialized[key] for key in sorted(materialized))


def _expand_schema_template(
    template: str,
    requirement: SchemaRequirement,
) -> Path:
    replacements = {
        "{{.Group}}": requirement.gvk.group,
        "{{.ResourceKind}}": requirement.gvk.kind.lower(),
        "{{.ResourceAPIVersion}}": requirement.gvk.version,
    }
    expanded = template
    for marker, value in replacements.items():
        expanded = expanded.replace(marker, value)
    return Path(expanded)


def build_repository_kubeconform_schema_service(
    *,
    workspace: RepositoryWorkspace,
    validation: ManifestValidationService,
    source: KubeconformSchemaSource,
    cache_root: Path | None = None,
) -> RepositoryKubeconformSchemaService:
    """Compose the high-level service without importing adapters into the CLI."""
    store = KubeconformSchemaStore(
        _require_workspace_name(workspace),
        cache_root=cache_root,
    )
    return RepositoryKubeconformSchemaService(
        workspace=workspace,
        validation=validation,
        sync=KubeconformSchemaSyncService(store, source),
    )


def _require_workspace_name(workspace: RepositoryWorkspace) -> str:
    if not workspace.name:
        raise KubeconformSchemaConfigurationError(
            f"{workspace.marker} must declare metadata.name before schemas can be synchronized"
        )
    return workspace.name


__all__ = [
    "RepositoryKubeconformSchemaService",
    "RepositoryKubeconformSchemaSyncResult",
    "build_repository_kubeconform_schema_service",
]
