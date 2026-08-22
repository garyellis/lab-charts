import yaml

from .conftest import REPO_ROOT


def _yaml(path: str) -> dict:
    content = (REPO_ROOT / path).read_text(encoding="utf-8")
    loaded = yaml.safe_load(content)
    assert isinstance(loaded, dict)
    return loaded


def test_prometheus_operator_is_controller_and_crds_without_full_stack() -> None:
    chart = _yaml("charts/prometheus-operator/Chart.yaml")
    values = _yaml("charts/prometheus-operator/values.yaml")
    upstream = values["prometheus-operator"]

    assert chart["version"] == "0.1.1"
    assert upstream["crds"]["enabled"] is True
    assert upstream["prometheusOperator"]["enabled"] is True
    assert upstream["defaultRules"]["create"] is False
    for component in (
        "prometheus",
        "alertmanager",
        "grafana",
        "thanosRuler",
        "nodeExporter",
        "kubeStateMetrics",
    ):
        assert upstream[component]["enabled"] is False

    assert values["serviceMonitors"]["cilium"]["enabled"] is True
    assert values["serviceMonitors"]["istio"]["enabled"] is True


def test_thanos_defaults_are_infrastructure_only_with_monitoring_definitions() -> None:
    chart = _yaml("charts/thanos/Chart.yaml")
    values = _yaml("charts/thanos/values.yaml")["thanos"]

    assert chart["version"] == "0.2.0"
    assert values["global"]["serviceMonitor"]["enabled"] is True
    assert values["global"]["thanosRules"]["enabled"] is True
    assert values["ruler"]["enabled"] is False
    assert values["queryFrontend"]["enabled"] is False
    assert values["kube-prometheus-stack"]["enabled"] is False
    assert values["rustfs"]["enabled"] is False

    assert values["receive"]["tsdb"]["retention"] == "48h"
    assert values["receive"]["persistence"]["size"] == "25Gi"
    assert values["storegateway"]["persistence"]["size"] == "10Gi"
    assert values["compactor"]["persistence"]["size"] == "20Gi"
    assert values["compactor"]["retention"] == {
        "resolutionRaw": "7d",
        "resolution5m": "14d",
        "resolution1h": "30d",
    }


def test_rustfs_defaults_require_external_secrets_and_one_data_disk() -> None:
    chart = _yaml("charts/rustfs/Chart.yaml")
    values_path = REPO_ROOT / "charts/rustfs/values.yaml"
    values = _yaml("charts/rustfs/values.yaml")
    upstream = values["rustfs"]
    bootstrap = values["bootstrap"]

    assert chart["version"] == "0.1.0"
    assert chart["dependencies"] == [
        {
            "name": "rustfs",
            "version": "0.12.0",
            "repository": "https://charts.rustfs.com/",
        }
    ]
    assert upstream["mode"]["standalone"]["enabled"] is True
    assert upstream["mode"]["distributed"]["enabled"] is False
    assert upstream["replicaCount"] == 1
    assert upstream["drivesPerNode"] == 1
    assert upstream["storageclass"]["dataStorageSize"] == "100Gi"
    assert upstream["config"]["rustfs"]["obs_log_directory"] == ""
    assert upstream["config"]["rustfs"]["console_enable"] == "false"
    assert upstream["service"]["type"] == "ClusterIP"
    assert upstream["ingress"]["enabled"] is False
    assert upstream["gatewayApi"]["enabled"] is False
    assert upstream["secret"]["existingSecret"] == "rustfs-root"

    assert bootstrap["bucket"] == "thanos-metrics"
    assert bootstrap["workloadSecret"]["name"] == "rustfs-thanos"
    assert bootstrap["workloadSecret"]["create"] is False
    assert bootstrap["image"]["tag"] != "latest"
    assert "rustfsadmin" not in values_path.read_text(encoding="utf-8")


def test_rustfs_bootstrap_reconciles_the_workload_secret_key() -> None:
    template = (
        REPO_ROOT / "charts/rustfs/templates/bootstrap-job.yaml"
    ).read_text(encoding="utf-8")

    update_command = """rc admin service-account update store "$WORKLOAD_ACCESS_KEY" \\
                  --secret-key "$WORKLOAD_SECRET_KEY" \\
                  --policy /policy/policy.json"""
    assert update_command in template


def test_rustfs_helm_test_uses_only_bucket_scoped_s3_operations() -> None:
    values = _yaml("charts/rustfs/values.yaml")
    template = (
        REPO_ROOT / "charts/rustfs/templates/tests/bucket-access.yaml"
    ).read_text(encoding="utf-8")

    image = values["tests"]["bucketAccess"]["image"]
    assert image == {
        "repository": "amazon/aws-cli",
        "tag": "2.31.0",
        "pullPolicy": "IfNotPresent",
    }
    assert "aws s3api put-object" in template
    assert "aws s3api get-object" in template
    assert "aws s3api delete-object" in template
    assert "aws s3api head-bucket" in template
    assert "--bucket \"$BUCKET\"" in template
    assert "rc alias set" not in template
    assert "list-buckets" not in template
    assert "AWS_ACCESS_KEY_ID" in template
    assert "readOnlyRootFilesystem: true" in template
    assert "runAsNonRoot: true" in template
