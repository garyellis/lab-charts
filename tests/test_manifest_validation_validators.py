from dataclasses import dataclass
from pathlib import Path

import pytest

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.manifest_validation.app import (
    ManifestValidationService,
    RunnerSpec,
)
from chart_manager.services.manifest_validation.catalog import (
    load_manifest_validation_target,
)
from chart_manager.services.manifest_validation.models import PhaseResult, WorklistRow
from chart_manager.services.manifest_validation.resolver import (
    resolve_manifest_validation,
    row_config_for,
)
from chart_manager.services.manifest_validation.runner import (
    ManifestValidationRunner,
    RowConfig,
)
from chart_manager.services.manifest_validation.validator_adapters import (
    KyvernoProvider,
)
from chart_manager.services.manifest_validation.validators import (
    ManifestValidator,
    ValidatorCategory,
    ValidatorCompileContext,
    ValidatorInvocation,
    validate_registry,
)


class _HelmStub:
    timeout = None

    def dependency_update(self, _chart: Path, *, timeout: float | None = None) -> None:
        return None

    def template(
        self,
        _release: str,
        _chart: Path,
        *,
        namespace: str,
        output_dir: Path,
        values: list[Path],
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")
        return output_dir


def _row() -> WorklistRow:
    return WorklistRow(
        chart="demo",
        env="dev",
        release="demo",
        namespace="default",
    )


@dataclass
class _Provider:
    validator_id: str
    category: ValidatorCategory
    order: int
    executor: ManifestValidator | None = None

    def compile(self, context: ValidatorCompileContext) -> ValidatorInvocation:
        return ValidatorInvocation(
            validator_id=self.validator_id,
            category=self.category,
            order=self.order,
            enabled=True,
            config={"strict": True},
        )

    def build(
        self,
        *,
        command_runner: CommandRunner,
        timeout: float | None,
    ) -> ManifestValidator:
        assert self.executor is not None
        return self.executor


def test_registry_rejects_duplicate_identity_and_order() -> None:
    base = _Provider("one", ValidatorCategory.SCHEMA, 10)

    with pytest.raises(ValueError, match="duplicate validator id"):
        validate_registry((base, _Provider("one", ValidatorCategory.POLICY, 20)))
    with pytest.raises(ValueError, match="duplicate validator order"):
        validate_registry((base, _Provider("two", ValidatorCategory.POLICY, 10)))


def test_registry_orders_definitions_deterministically() -> None:
    definitions = validate_registry(
        (
            _Provider("later", ValidatorCategory.POLICY, 20),
            _Provider("first", ValidatorCategory.SCHEMA, 10),
        )
    )

    assert [definition.validator_id for definition in definitions] == ["first", "later"]


def test_third_validator_uses_shared_runner_without_orchestrator_branch(
    tmp_path: Path,
) -> None:
    class ThirdValidator:
        def __init__(self) -> None:
            self.calls: list[Path] = []

        def validate(self, rendered_dir: Path, config: object) -> PhaseResult:
            self.calls.append(rendered_dir)
            assert config == {"strict": True}
            return PhaseResult(phase="policy", status="PASS", detail="third validator passed")

    third = ThirdValidator()
    provider = _Provider(
        "third",
        ValidatorCategory.POLICY,
        300,
        third,
    )
    chart = tmp_path / "charts" / "demo"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: demo\nversion: 0.1.0\n")
    (chart / "values.yaml").write_text("{}\n")
    (chart / "chart-lifecycle.yaml").write_text(
        "apiVersion: lifecycle.chartmanager.io/v1alpha1\n"
        "kind: ChartLifecycle\n"
        "metadata:\n"
        "  name: demo\n"
        "spec:\n"
        "  validation:\n"
        "    releaseName: demo\n"
        "    environments:\n"
        "      dev:\n"
        "        namespace: default\n"
        "        values: [values.yaml]\n"
    )
    target = load_manifest_validation_target(tmp_path, "demo")
    compiled = resolve_manifest_validation(
        target,
        tmp_path,
        providers=(provider,),
    )
    row = _row()
    config = row_config_for(compiled, row)
    service = ManifestValidationService(
        validator_providers=(provider,),
        command_runner=object(),  # type: ignore[arg-type]
    )
    runner = service._build_runner(
        RunnerSpec(
            output_root=tmp_path / "out",
            validator_ids=frozenset({"third"}),
        )
    )
    runner.helm_factory = lambda _version, _binary: _HelmStub()  # type: ignore[assignment]

    result = runner.run([config])

    assert len(third.calls) == 1
    assert result.rows[0].phases["schema"].status == "SKIP"
    assert result.rows[0].phases["policy"].status == "PASS"
    assert result.rows[0].validator_results["third"].detail == "third validator passed"

    coexisting = _Provider(
        "third",
        ValidatorCategory.POLICY,
        300,
        third,
    )
    providers = (KyvernoProvider(), coexisting)
    compiled = resolve_manifest_validation(
        target,
        tmp_path,
        providers=providers,
    )
    service = ManifestValidationService(
        validator_providers=providers,
        command_runner=object(),  # type: ignore[arg-type]
    )
    runner = service._build_runner(
        RunnerSpec(
            output_root=tmp_path / "out-coexisting",
            validator_ids=frozenset({"kyverno", "third"}),
        )
    )
    runner.helm_factory = lambda _version, _binary: _HelmStub()  # type: ignore[assignment]

    result = runner.run([row_config_for(compiled, row)])

    assert result.rows[0].validator_results["kyverno"].status == "SKIP"
    assert result.rows[0].validator_results["third"].status == "PASS"
    assert result.rows[0].phases["policy"].status == "PASS"


@pytest.mark.parametrize(
    ("enabled", "expected_status", "expected_outcome"),
    [(False, "SKIP", Outcome.SUCCESS), (True, "FAIL", Outcome.TOOL)],
)
def test_disabled_validator_needs_no_executor_but_enabled_missing_executor_fails(
    tmp_path: Path,
    enabled: bool,
    expected_status: str,
    expected_outcome: Outcome,
) -> None:
    invocation = ValidatorInvocation(
        validator_id="third",
        category=ValidatorCategory.POLICY,
        order=300,
        enabled=enabled,
        config=object(),
    )
    runner = ManifestValidationRunner(
        helm_factory=lambda _version, _binary: _HelmStub(),  # type: ignore[arg-type]
        output_root=tmp_path / "out",
        validators={},
    )

    result = runner.run(
        [
            RowConfig(
                row=_row(),
                chart_path=tmp_path / "chart",
                validator_invocations=(invocation,),
            )
        ]
    )

    assert result.rows[0].phases["policy"].status == expected_status
    assert result.outcome() is expected_outcome
