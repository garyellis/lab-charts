from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.kubeconform import (
    Kubeconform,
    KubeconformReport,
    ResourceResult,
)
from chart_manager.integrations.kyverno import Kyverno, KyvernoReport, PolicyResult
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.validate_models import WorklistRow
from chart_manager.services.validate import phases


class _StubKubeconform(Kubeconform):
    def __init__(self, *, report: KubeconformReport | None = None, raise_exc: Exception | None = None) -> None:
        # Skip parent __init__ — we don't need a runner or a resolved binary.
        self._report = report
        self._raise = raise_exc
        self.validate_calls: list[dict] = []

    def validate(  # type: ignore[override]
        self,
        manifests_dir: Path,
        *,
        kubernetes_version: str | None = None,
        schema_locations: list[str] | None = None,
        skip_kinds: list[str] | None = None,
        strict: bool = True,
        extra_args: list[str] | None = None,
    ) -> KubeconformReport:
        self.validate_calls.append(
            {
                "manifests_dir": manifests_dir,
                "kubernetes_version": kubernetes_version,
                "schema_locations": schema_locations,
            }
        )
        if self._raise is not None:
            raise self._raise
        assert self._report is not None
        return self._report


def _row() -> WorklistRow:
    return WorklistRow(chart="demo", env="dev", release="demo", namespace="lab-dev")


def _seed_manifest(dir_: Path) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "deploy.yaml").write_text("kind: Deployment\n")


def test_schema_pass_returns_pass(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    report = KubeconformReport(resources=(), summary={"valid": 1, "invalid": 0, "errors": 0, "skipped": 0})
    kc = _StubKubeconform(report=report)

    result = phases.schema(_row(), kubeconform=kc, rendered_dir=tmp_path)

    assert result.status == "PASS"
    assert result.error_type is None
    assert result.phase == "schema"


def test_schema_fail_formats_findings_one_per_line(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    report = KubeconformReport(
        resources=(
            ResourceResult(
                filename="/r/deploy.yaml",
                kind="Deployment",
                name="bad",
                status="invalid",
                msg="/spec/replicas: got string, want integer",
            ),
            ResourceResult(
                filename="/r/svc.yaml",
                kind="Service",
                name="bad-svc",
                status="error",
                msg="schema fetch failed",
            ),
        ),
        summary={"valid": 0, "invalid": 1, "errors": 1, "skipped": 0},
    )
    kc = _StubKubeconform(report=report)

    result = phases.schema(_row(), kubeconform=kc, rendered_dir=tmp_path)

    assert result.status == "FAIL"
    assert result.error_type is None  # spec/chart-author failure, not a tool crash
    assert result.detail is not None
    lines = result.detail.split("\n")
    assert len(lines) == 2
    assert "Deployment/bad" in lines[0]
    assert "/spec/replicas" in lines[0]
    assert "Service/bad-svc" in lines[1]


def test_schema_tool_crash_returns_fail_with_tool_error_type(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    kc = _StubKubeconform(raise_exc=ExternalCommandError("kubeconform exploded"))

    result = phases.schema(_row(), kubeconform=kc, rendered_dir=tmp_path)

    assert result.status == "FAIL"
    assert result.error_type == "tool"
    assert "kubeconform exploded" in (result.detail or "")


def test_schema_empty_dir_returns_skip(tmp_path: Path) -> None:
    kc = _StubKubeconform(report=KubeconformReport(resources=(), summary={}))

    # rendered_dir exists but contains no yaml/yml files.
    empty = tmp_path / "empty"
    empty.mkdir()
    result = phases.schema(_row(), kubeconform=kc, rendered_dir=empty)

    assert result.status == "SKIP"
    assert result.detail == "no manifests"
    assert kc.validate_calls == []


def test_schema_missing_dir_returns_skip(tmp_path: Path) -> None:
    kc = _StubKubeconform(report=KubeconformReport(resources=(), summary={}))

    result = phases.schema(_row(), kubeconform=kc, rendered_dir=tmp_path / "does-not-exist")

    assert result.status == "SKIP"
    assert result.detail == "no manifests"


def test_schema_skips_when_only_non_yaml_files_present(tmp_path: Path) -> None:
    # A rendered dir that contains files but no .yaml/.yml should still SKIP.
    rendered = tmp_path / "out"
    rendered.mkdir()
    (rendered / "NOTES.txt").write_text("post-install notes")
    kc = _StubKubeconform(report=KubeconformReport(resources=(), summary={}))

    result = phases.schema(_row(), kubeconform=kc, rendered_dir=rendered)

    assert result.status == "SKIP"
    assert kc.validate_calls == []


def test_schema_skips_through_cyclic_symlink_without_hanging(tmp_path: Path) -> None:
    # Guards against an accidental rglob() regression that would follow
    # symlinked directories and loop indefinitely on a cycle.
    rendered = tmp_path / "out"
    rendered.mkdir()
    # cycle: rendered/loop -> rendered
    (rendered / "loop").symlink_to(rendered, target_is_directory=True)
    kc = _StubKubeconform(report=KubeconformReport(resources=(), summary={}))

    result = phases.schema(_row(), kubeconform=kc, rendered_dir=rendered)

    # No yaml files anywhere -> SKIP; the important part is that the walk
    # terminates rather than recursing into the symlinked cycle.
    assert result.status == "SKIP"


class _StubKyverno(Kyverno):
    def __init__(
        self,
        *,
        report: KyvernoReport | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._report = report
        self._raise = raise_exc
        self.apply_calls: list[dict] = []

    def apply(  # type: ignore[override]
        self,
        manifests_dir: Path,
        *,
        policy_paths: list[Path],
        extra_args: list[str] | None = None,
    ) -> KyvernoReport:
        self.apply_calls.append({"manifests_dir": manifests_dir, "policy_paths": policy_paths})
        if self._raise is not None:
            raise self._raise
        assert self._report is not None
        return self._report


def test_policy_pass_returns_pass(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    ky = _StubKyverno(report=KyvernoReport(results=(), summary={"pass": 1, "fail": 0}))

    result = phases.policy(
        _row(),
        kyverno=ky,
        rendered_dir=tmp_path,
        policy_paths=[tmp_path / "policies"],
    )

    assert result.status == "PASS"
    assert result.error_type is None
    assert result.phase == "policy"


def test_policy_fail_formats_findings_one_per_line(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    ky = _StubKyverno(
        report=KyvernoReport(
            results=(
                PolicyResult(
                    policy="require-non-root",
                    rule="containers-must-run-as-non-root",
                    resource_kind="Deployment",
                    resource_name="bad",
                    resource_namespace="default",
                    status="fail",
                    message="validation error: runAsNonRoot",
                ),
                PolicyResult(
                    policy="forbid-load-balancer",
                    rule="service-type-not-load-balancer",
                    resource_kind="Service",
                    resource_name="svc",
                    resource_namespace="default",
                    status="fail",
                    message="LoadBalancer not allowed",
                ),
            ),
            summary={"pass": 0, "fail": 2},
        )
    )

    result = phases.policy(
        _row(), kyverno=ky, rendered_dir=tmp_path, policy_paths=[Path("/p")]
    )

    assert result.status == "FAIL"
    assert result.error_type is None  # spec/chart-author failure, not tool crash
    assert result.detail is not None
    lines = result.detail.split("\n")
    assert len(lines) == 2
    assert "require-non-root/containers-must-run-as-non-root" in lines[0]
    assert "Deployment/bad" in lines[0]
    assert "forbid-load-balancer/service-type-not-load-balancer" in lines[1]


def test_policy_tool_crash_returns_fail_with_tool_error_type(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    ky = _StubKyverno(raise_exc=ExternalCommandError("kyverno exploded"))

    result = phases.policy(
        _row(), kyverno=ky, rendered_dir=tmp_path, policy_paths=[Path("/p")]
    )

    assert result.status == "FAIL"
    assert result.error_type == "tool"
    assert "kyverno exploded" in (result.detail or "")


def test_policy_empty_policy_paths_returns_skip(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    ky = _StubKyverno(report=KyvernoReport(results=(), summary={}))

    result = phases.policy(_row(), kyverno=ky, rendered_dir=tmp_path, policy_paths=[])

    assert result.status == "SKIP"
    assert result.detail == "no policies discovered"
    assert ky.apply_calls == []


def test_policy_empty_rendered_dir_returns_skip(tmp_path: Path) -> None:
    ky = _StubKyverno(report=KyvernoReport(results=(), summary={}))
    empty = tmp_path / "empty"
    empty.mkdir()

    result = phases.policy(
        _row(), kyverno=ky, rendered_dir=empty, policy_paths=[Path("/p")]
    )

    assert result.status == "SKIP"
    assert result.detail == "no manifests"
    assert ky.apply_calls == []


def test_policy_missing_rendered_dir_returns_skip(tmp_path: Path) -> None:
    ky = _StubKyverno(report=KyvernoReport(results=(), summary={}))

    result = phases.policy(
        _row(),
        kyverno=ky,
        rendered_dir=tmp_path / "does-not-exist",
        policy_paths=[Path("/p")],
    )

    assert result.status == "SKIP"
    assert result.detail == "no manifests"


def test_policy_warn_only_passes_with_advisory_detail(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    ky = _StubKyverno(
        report=KyvernoReport(
            results=(
                PolicyResult(
                    policy="warn-only",
                    rule="r",
                    resource_kind="Pod",
                    resource_name="p",
                    resource_namespace=None,
                    status="warn",
                    message="non-fatal advisory",
                ),
            ),
            summary={"pass": 0, "fail": 0, "warn": 1},
        )
    )

    result = phases.policy(
        _row(), kyverno=ky, rendered_dir=tmp_path, policy_paths=[Path("/p")]
    )

    assert result.status == "PASS"
    assert result.detail is not None
    assert result.detail.startswith("warnings:")
    assert "warn-only/r" in result.detail
    assert "non-fatal advisory" in result.detail


def test_policy_fail_with_warns_includes_both_in_detail(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    ky = _StubKyverno(
        report=KyvernoReport(
            results=(
                PolicyResult(
                    policy="must-not-root",
                    rule="r1",
                    resource_kind="Deployment",
                    resource_name="d",
                    resource_namespace=None,
                    status="fail",
                    message="root forbidden",
                ),
                PolicyResult(
                    policy="advisory",
                    rule="r2",
                    resource_kind="Service",
                    resource_name="s",
                    resource_namespace=None,
                    status="warn",
                    message="prefer ClusterIP",
                ),
            ),
            summary={"pass": 0, "fail": 1, "warn": 1},
        )
    )

    result = phases.policy(
        _row(), kyverno=ky, rendered_dir=tmp_path, policy_paths=[Path("/p")]
    )

    assert result.status == "FAIL"
    assert result.detail is not None
    assert "must-not-root/r1" in result.detail
    assert "root forbidden" in result.detail
    assert "warnings:" in result.detail
    assert "advisory/r2" in result.detail
    assert "prefer ClusterIP" in result.detail


def test_schema_passes_overrides_through_to_kubeconform(tmp_path: Path) -> None:
    _seed_manifest(tmp_path)
    kc = _StubKubeconform(report=KubeconformReport(resources=(), summary={}))

    phases.schema(
        _row(),
        kubeconform=kc,
        rendered_dir=tmp_path,
        kubernetes_version="1.31.2",
        schema_locations=["/local/schemas"],
    )

    assert kc.validate_calls[0]["kubernetes_version"] == "1.31.2"
    assert kc.validate_calls[0]["schema_locations"] == ["/local/schemas"]
