# Node Problem Detector

This umbrella chart deploys Kubernetes Node Problem Detector (NPD) through the
Delivery Hero chart. It runs one privileged pod per node, including tainted
control-plane nodes, and reports detected kernel and systemd problems through
node conditions, events, and Prometheus metrics.

## Versions

| Component | Version |
| --- | --- |
| Delivery Hero chart | `2.4.1` |
| NPD image | `v1.35.3` (multi-architecture digest pinned) |

The image override contains the security fixes released after the dependency's
default `v1.35.1` image.

## Configuration

Dependency values must remain under the `node-problem-detector` key. The default
profile enables kernel, read-only filesystem, and systemd journal monitoring.
Docker monitoring is intentionally disabled because the lab clusters use
containerd. ServiceMonitor and PrometheusRule resources are enabled, so the
Prometheus Operator CRDs must exist before installation.

The production profile mounts the host's `/dev/kmsg` device for the kernel and
read-only filesystem monitors. The CI overlay uses only the systemd monitor,
mapping kind's `/run/log/journal` host directory to `/var/log/journal` in the
pod. Kind does not provide a representative host kernel device.

Privileged execution is intentional: NPD reads host kernel messages and systemd
journal data. For the same reason, policy validation is disabled in
`chart-lifecycle.yaml`; rendering and schema validation remain enabled.

## Local validation

```sh
helm dependency update charts/node-problem-detector
helm template node-problem-detector charts/node-problem-detector \
  --namespace node-problem-detector \
  -f charts/node-problem-detector/values.yaml \
  -f charts/node-problem-detector/values-ci.yaml
```

The lifecycle `minimal` profile installs the Prometheus Operator prerequisite,
deploys NPD into the dedicated `node-problem-detector` namespace, and runs a
Helm test that waits for the DaemonSet rollout and confirms the pods remain
ready after startup. It does not inject node faults.
