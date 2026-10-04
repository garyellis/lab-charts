"""End-to-end manifest render -> schema integration test.

Skips cleanly if helm or kubeconform are not on PATH so unit-test runs on
contributor machines without the validate tooling stay green. Local and CI
tool installation is owned by mise.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.plumbing.commands import SubprocessRunner
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.services.manifest_validation.models import WorklistRow
from chart_manager.services.manifest_validation.runner import ManifestValidationRunner, RowConfig
from chart_manager.services.manifest_validation.validator_adapters import (
    KubeconformValidator,
)
from chart_manager.services.manifest_validation.validators import (
    KubeconformConfig,
    KyvernoConfig,
    ValidatorCategory,
    ValidatorInvocation,
)

pytestmark = pytest.mark.integration

FIXTURE_CHARTS = Path(__file__).parent.parent / "fixtures" / "charts"
FIXTURE_SCHEMAS = Path(__file__).parent.parent / "fixtures" / "schemas"
SCHEMA_LOCATION = str(
    FIXTURE_SCHEMAS / "{{.Group}}" / "{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)


def _skip_if_missing(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        pytest.skip(f"missing tools on PATH: {', '.join(missing)}")


def _runner(out_root: Path) -> ManifestValidationRunner:
    cmd_runner = SubprocessRunner()
    helm = Helm(runner=cmd_runner)
    return ManifestValidationRunner(
        helm_factory=lambda _version, _binary: helm,
        output_root=out_root,
        validators={
            "kubeconform": KubeconformValidator(Kubeconform(runner=cmd_runner)),
        },
    )


def _inputs(chart_dir: Path, *, env: str = "dev") -> RowConfig:
    row = WorklistRow(
        chart=chart_dir.name,
        env=env,
        release=chart_dir.name,
        namespace=f"lab-{env}",
    )
    values = [chart_dir / "values.yaml"] if (chart_dir / "values.yaml").is_file() else []
    return RowConfig(
        row=row,
        chart_path=chart_dir,
        values=values,
        validator_invocations=(
            ValidatorInvocation(
                validator_id="kubeconform",
                category=ValidatorCategory.SCHEMA,
                order=100,
                enabled=True,
                config=KubeconformConfig(None, (SCHEMA_LOCATION,)),
            ),
            ValidatorInvocation(
                validator_id="kyverno",
                category=ValidatorCategory.POLICY,
                order=200,
                enabled=False,
                config=KyvernoConfig(()),
            ),
        ),
    )


def test_passing_app_renders_and_passes_schema(tmp_path: Path) -> None:
    _skip_if_missing("helm", "kubeconform")
    chart = FIXTURE_CHARTS / "passing-app"

    result = _runner(tmp_path / "out").run([_inputs(chart)])

    row = result.rows[0]
    assert row.phases["render"].status == "PASS"
    assert row.phases["schema"].status == "PASS", row.phases["schema"].detail
    assert result.outcome() is Outcome.SUCCESS


def test_schema_violator_renders_and_fails_schema(tmp_path: Path) -> None:
    _skip_if_missing("helm", "kubeconform")
    chart = FIXTURE_CHARTS / "schema-violator"

    result = _runner(tmp_path / "out").run([_inputs(chart)])

    row = result.rows[0]
    assert row.phases["render"].status == "PASS"
    assert row.phases["schema"].status == "FAIL"
    detail = row.phases["schema"].detail or ""
    assert "Deployment/schema-violator" in detail
    assert "/spec/replicas" in detail
    assert result.outcome() is Outcome.FAILED


def test_real_missing_schema_reports_remediation(tmp_path: Path) -> None:
    _skip_if_missing("kubeconform")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "widget.yaml").write_text(
        "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\n"
    )
    result = KubeconformValidator(Kubeconform()).validate(
        manifests,
        KubeconformConfig(None, (str(tmp_path / "missing.json"),)),
    )
    assert result.status == "FAIL"
    assert result.error_type == "tool"
    assert "could not find schema" in result.detail
    assert "chart-local schema" in result.detail


def test_new_kinds_and_changed_crds_validate_without_sync_or_lock_changes(tmp_path, monkeypatch, caplog):
    from chart_manager.commands.validate.schemas.lock import write_schema_lock_atomic
    from chart_manager.commands.validate.schemas.store import KubeconformSchemaStore
    from chart_manager.services.manifest_validation.app import ManifestValidationService
    from chart_manager.services.manifest_validation.models import RunRequest
    from tests.schema_fixtures import schema_store, workspace
    from tests.test_kubeconform_schema_generated import chart
    from tests.test_kubeconform_schema_inventory_crd import _crd

    _skip_if_missing("helm", "kubeconform", "git")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    lock, _, snapshots = schema_store(tmp_path)
    store = KubeconformSchemaStore(snapshots=snapshots)
    store.sync(lock)
    lock_path = tmp_path / ".chart-manager/schemas.lock.yaml"
    write_schema_lock_atomic(lock_path, lock)
    before = lock_path.read_bytes()
    provider = chart(tmp_path, "provider", _crd())
    consumer = chart(
        tmp_path,
        "consumer",
        "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\nspec: {name: hello}\n",
    )
    unrelated = chart(tmp_path, "unrelated", "apiVersion: v1\nkind: ConfigMap\n")
    (unrelated / "values.yaml").write_text("[broken YAML")
    service = ManifestValidationService(workspace=workspace(tmp_path))

    def validate():
        result = service.run(
            RunRequest(
                root=tmp_path,
                charts=("consumer",),
                phases=frozenset({"render", "schema"}),
            )
        ).result
        assert not result.spec_errors, result.spec_errors
        assert lock_path.read_bytes() == before
        assert len(snapshots.calls) == 2
        return result

    with caplog.at_level(logging.DEBUG):
        assert validate().outcome() is Outcome.SUCCESS
    messages = [
        record for record in caplog.records
        if record.name.startswith("chart_manager.services.manifest_validation")
    ]
    visible = [record.getMessage() for record in messages if record.levelno == logging.INFO]
    assert len(visible) == 3
    assert visible[0] == "Checking cached upstream schemas"
    assert visible[1].startswith("Validating 1 rows across 1 charts")
    assert visible[2].startswith("Validation finished: rows=1 failed=0")
    assert "preparation=" in visible[2]
    assert any(
        record.levelno == logging.DEBUG and "validate run started" in record.getMessage()
        for record in messages
    )
    assert any(
        record.levelno == logging.INFO
        and record.getMessage() == "Preparing CRD schemas: 0 providers cached, 1 to render"
        for record in caplog.records
    )
    # A newly introduced built-in kind comes from the full pinned snapshot.
    (consumer / "templates/new.yaml").write_text(
        "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata: {name: demo}\n"
    )
    assert validate().outcome() is Outcome.SUCCESS
    # Current generated CRDs override the permissive catalog schema immediately.
    (provider / "templates/resources.yaml").write_text(_crd(nested_type="integer"))
    result = validate()
    assert result.outcome() is Outcome.FAILED
    assert "want integer" in result.rows[0].phases["schema"].detail
    (consumer / "templates/resources.yaml").write_text(
        "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\nspec: {name: 12}\n"
    )
    assert validate().outcome() is Outcome.SUCCESS
