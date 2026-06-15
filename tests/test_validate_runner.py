from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import (
    Kubeconform,
    KubeconformReport,
    ResourceResult,
)
from chart_manager.integrations.kyverno import Kyverno, KyvernoReport, PolicyResult
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.validate_models import WorklistRow
from chart_manager.services.validate.runner import RowConfig, ValidateRunner


class _StubHelm(Helm):
    def __init__(self, *, succeed: bool, raise_exc: Exception | None = None) -> None:
        # Skip parent __init__: don't construct a CommandRunner or resolve a binary.
        self._succeed = succeed
        self._raise = raise_exc
        self.calls: list[dict] = []
        self.dep_update_calls: list[Path] = []

    def dependency_update(
        self, chart_path: Path, *, timeout: float | None = None
    ) -> None:  # type: ignore[override]
        # Stub the runner's dep-prefetch pass: track calls, don't shell out.
        _ = timeout  # accepted for signature parity with the real Helm.
        self.dep_update_calls.append(chart_path.resolve())

    def template(  # type: ignore[override]
        self,
        release: str,
        chart_ref,
        *,
        namespace: str,
        output_dir: Path,
        values=None,
        sets=None,
        api_versions=None,
        kube_version=None,
        skip_tests: bool = True,
    ) -> Path:
        self.calls.append({"release": release, "output_dir": output_dir})
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if self._raise is not None:
            raise self._raise
        if not self._succeed:
            raise ExternalCommandError("helm fake failure")
        # Seed a manifest so downstream phases find something.
        (output_dir / "rendered.yaml").write_text("kind: Deployment\n")
        return output_dir


class _StubKubeconform(Kubeconform):
    def __init__(self, report: KubeconformReport) -> None:
        self._report = report
        self.calls: list[Path] = []

    def validate(  # type: ignore[override]
        self,
        manifests_dir: Path,
        *,
        kubernetes_version=None,
        schema_locations=None,
        skip_kinds=None,
        strict: bool = True,
        extra_args=None,
    ) -> KubeconformReport:
        self.calls.append(manifests_dir)
        return self._report


class _StubKyverno(Kyverno):
    def __init__(
        self,
        *,
        report: KyvernoReport | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        # Skip parent __init__ — no runner or binary needed.
        self._report = report
        self._raise = raise_exc
        self.calls: list[dict] = []

    def apply(  # type: ignore[override]
        self,
        manifests_dir: Path,
        *,
        policy_paths: list[Path],
        extra_args=None,
    ) -> KyvernoReport:
        self.calls.append({"manifests_dir": manifests_dir, "policy_paths": policy_paths})
        if self._raise is not None:
            raise self._raise
        assert self._report is not None
        return self._report


def _row(chart: str = "demo") -> WorklistRow:
    return WorklistRow(chart=chart, env="dev", release=chart, namespace="lab-dev")


def _cfg(
    row: WorklistRow,
    chart_path: Path,
    *,
    policy_paths: list[Path] | None = None,
    kubernetes_version: str | None = None,
    schema_locations: list[str] | None = None,
) -> RowConfig:
    return RowConfig(
        row=row,
        chart_path=chart_path,
        values=[],
        kubernetes_version=kubernetes_version,
        schema_locations=schema_locations,
        policy_paths=policy_paths,
    )


def _ok_report() -> KubeconformReport:
    return KubeconformReport(
        resources=(),
        summary={"valid": 1, "invalid": 0, "errors": 0, "skipped": 0},
    )


def _kyverno_pass() -> KyvernoReport:
    return KyvernoReport(results=(), summary={"pass": 1, "fail": 0})


def test_render_pass_triggers_schema_phase(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    runner = ValidateRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    row = _row()
    # No policy_paths -> policy SKIPs cleanly (no policies discovered).
    result = runner.run([_cfg(row, tmp_path / "chart")])

    assert len(kc.calls) == 1
    row_result = result.rows[0]
    assert row_result.phases["render"].status == "PASS"
    assert row_result.phases["schema"].status == "PASS"
    assert row_result.phases["policy"].status == "SKIP"
    assert row_result.phases["policy"].detail == "no policies discovered"
    assert result.exit_code() == 0


def test_render_fail_skips_schema_and_policy_with_upstream_detail(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=False)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ValidateRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    row = _row()
    result = runner.run([_cfg(row, tmp_path / "chart", policy_paths=[Path("/p")])])

    assert kc.calls == []
    assert ky.calls == []
    row_result = result.rows[0]
    assert row_result.phases["render"].status == "FAIL"
    assert row_result.phases["render"].error_type == "tool"
    assert row_result.phases["schema"].status == "SKIP"
    assert row_result.phases["schema"].detail == "upstream render FAIL"
    assert row_result.phases["policy"].status == "SKIP"
    assert row_result.phases["policy"].detail == "upstream render FAIL"
    # Render tool crash promotes the run to exit code 2.
    assert result.exit_code() == 2


def test_schema_fail_skips_policy_with_upstream_detail(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(
        KubeconformReport(
            resources=(
                ResourceResult(
                    filename="/r/x.yaml",
                    kind="Deployment",
                    name="bad",
                    status="invalid",
                    msg="/spec/replicas: got string, want integer",
                ),
            ),
            summary={"valid": 0, "invalid": 1, "errors": 0, "skipped": 0},
        )
    )
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ValidateRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    result = runner.run([_cfg(_row(), tmp_path / "chart", policy_paths=[Path("/p")])])

    assert ky.calls == []
    row_result = result.rows[0]
    assert row_result.phases["schema"].status == "FAIL"
    assert row_result.phases["policy"].status == "SKIP"
    assert row_result.phases["policy"].detail == "upstream schema FAIL"
    assert result.exit_code() == 1


def test_policy_runs_after_passing_schema(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ValidateRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    policy_paths = [tmp_path / "policies"]
    (tmp_path / "policies").mkdir()
    result = runner.run([_cfg(_row(), tmp_path / "chart", policy_paths=policy_paths)])

    assert len(ky.calls) == 1
    assert ky.calls[0]["policy_paths"] == policy_paths
    row_result = result.rows[0]
    assert row_result.phases["policy"].status == "PASS"
    assert result.exit_code() == 0


def test_policy_failure_yields_exit_one(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
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
            ),
            summary={"pass": 0, "fail": 1},
        )
    )
    runner = ValidateRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    result = runner.run([_cfg(_row(), tmp_path / "chart", policy_paths=[Path("/p")])])

    policy_phase = result.rows[0].phases["policy"]
    assert policy_phase.status == "FAIL"
    assert policy_phase.error_type is None
    assert "require-non-root/containers-must-run-as-non-root" in (policy_phase.detail or "")
    assert "Deployment/bad" in (policy_phase.detail or "")
    assert result.exit_code() == 1


def test_runner_does_not_fail_fast_across_rows(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(
        KubeconformReport(
            resources=(
                ResourceResult(
                    filename="/r/x.yaml", kind="Deployment", name="bad",
                    status="invalid", msg="boom",
                ),
            ),
            summary={"valid": 0, "invalid": 1, "errors": 0, "skipped": 0},
        )
    )
    runner = ValidateRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    rows = [_row("a"), _row("b")]
    configs = [_cfg(r, tmp_path / "chart") for r in rows]
    result = runner.run(configs)

    assert len(result.rows) == 2
    assert len(kc.calls) == 2
    assert all(rr.phases["schema"].status == "FAIL" for rr in result.rows)


def test_phases_subset_marks_disabled_as_not_run(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    ky = _StubKyverno(report=_kyverno_pass())
    runner = ValidateRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, kyverno=ky
    )

    result = runner.run(
        [_cfg(_row(), tmp_path / "chart", policy_paths=[tmp_path / "p"])],
        enabled_phases=frozenset({"render", "schema"}),
    )

    row_result = result.rows[0]
    assert row_result.phases["render"].status == "PASS"
    assert row_result.phases["schema"].status == "PASS"
    assert row_result.phases["policy"].status == "NOT_RUN"
    assert ky.calls == []
    assert result.exit_code() == 0


def test_phases_subset_multi_row_batch(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    runner = ValidateRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    configs = [
        _cfg(_row("a"), tmp_path / "chart-a"),
        _cfg(_row("b"), tmp_path / "chart-b"),
        _cfg(_row("c"), tmp_path / "chart-c"),
    ]
    result = runner.run(configs)

    assert len(result.rows) == 3
    assert all(rr.phases["render"].status == "PASS" for rr in result.rows)
    assert all(rr.phases["schema"].status == "PASS" for rr in result.rows)
    assert result.exit_code() == 0


def test_parallel_run_returns_all_rows_with_events(tmp_path: Path) -> None:
    helm = _StubHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    events: list[tuple[str, str, str, bool]] = []

    def on_event(row, phase, status, elapsed_s):
        events.append((row.chart, phase, status, elapsed_s is not None))

    runner = ValidateRunner(
        helm=helm,
        output_root=tmp_path / "out",
        kubeconform=kc,
        max_workers=4,
        on_event=on_event,
    )
    configs = [
        _cfg(_row(f"chart-{i}"), tmp_path / f"chart-{i}") for i in range(6)
    ]
    result = runner.run(configs)

    assert len(result.rows) == 6
    # Deterministic sort by (chart, env) regardless of completion order.
    assert [r.row.chart for r in result.rows] == sorted(r.row.chart for r in result.rows)
    # 3 phases (render, schema, policy) x 6 rows x 2 events (running + end)
    # = 36. Policy SKIPs on `no policies discovered`, but the runner still
    # times it and emits both events.
    assert len(events) == 36
    # Every end-event carries an elapsed measurement.
    end_events = [e for e in events if e[2] != "running"]
    assert all(e[3] for e in end_events)


def test_parallel_run_isolates_worker_crash_into_row_failure(tmp_path: Path) -> None:
    boom = RuntimeError("kaboom")

    class _ExplodingHelm(_StubHelm):
        def template(self, release, chart_ref, **kwargs):  # type: ignore[override]
            if release == "bad":
                raise boom
            return super().template(release, chart_ref, **kwargs)

    helm = _ExplodingHelm(succeed=True)
    kc = _StubKubeconform(_ok_report())
    runner = ValidateRunner(
        helm=helm, output_root=tmp_path / "out", kubeconform=kc, max_workers=2
    )

    rows = [_row("good"), _row("bad")]
    configs = [_cfg(r, tmp_path / r.chart) for r in rows]
    result = runner.run(configs)

    # Both rows present; the crash converts to a tool-error FAIL render row.
    assert len(result.rows) == 2
    by_chart = {r.row.chart: r for r in result.rows}
    assert by_chart["bad"].phases["render"].status == "FAIL"
    assert by_chart["bad"].phases["render"].error_type == "tool"
    # Phase fns ARE called by _run_row, not _crash_row, so an in-phase
    # ExternalCommandError surfaces here. But this test uses a bare
    # RuntimeError, which the phase fn re-raises, escaping to the
    # worker. Verify the crash text bubbled into the detail.
    detail = by_chart["bad"].phases["render"].detail or ""
    assert "kaboom" in detail or "worker crashed" in detail
    # The good row still passes.
    assert by_chart["good"].phases["render"].status == "PASS"
    # Crash row also short-circuits schema/policy to SKIP.
    assert by_chart["bad"].phases["schema"].status == "SKIP"
    assert by_chart["bad"].phases["policy"].status == "SKIP"


def test_serial_path_when_max_workers_one(tmp_path: Path) -> None:
    # Sentinel: max_workers=1 must NOT use a ThreadPoolExecutor (the
    # original execution shape). We assert by patching the import site;
    # a call to ThreadPoolExecutor in the serial path would surface here.
    import chart_manager.services.validate.runner as runner_mod

    calls = []
    real_pool = runner_mod.ThreadPoolExecutor

    class _TrackingPool(real_pool):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            calls.append("pool")
            super().__init__(*a, **kw)

    runner_mod.ThreadPoolExecutor = _TrackingPool  # type: ignore[assignment]
    try:
        helm = _StubHelm(succeed=True)
        kc = _StubKubeconform(_ok_report())
        runner = ValidateRunner(
            helm=helm, output_root=tmp_path / "out", kubeconform=kc, max_workers=1
        )
        runner.run([_cfg(_row(), tmp_path / "c")])
    finally:
        runner_mod.ThreadPoolExecutor = real_pool  # type: ignore[assignment]
    assert calls == []


def test_schema_inputs_threaded_into_kubeconform(tmp_path: Path) -> None:
    captured: dict = {}

    class _CapturingKubeconform(_StubKubeconform):
        def validate(self, manifests_dir, *, kubernetes_version=None, schema_locations=None,
                     skip_kinds=None, strict=True, extra_args=None):
            captured["kubernetes_version"] = kubernetes_version
            captured["schema_locations"] = schema_locations
            return super().validate(manifests_dir)

    kc = _CapturingKubeconform(_ok_report())
    helm = _StubHelm(succeed=True)
    runner = ValidateRunner(helm=helm, output_root=tmp_path / "out", kubeconform=kc)

    runner.run(
        [
            _cfg(
                _row(),
                tmp_path / "chart",
                kubernetes_version="1.31.2",
                schema_locations=["/local"],
            )
        ]
    )

    assert captured["kubernetes_version"] == "1.31.2"
    assert captured["schema_locations"] == ["/local"]
