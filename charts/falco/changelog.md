# changelog

## 0.1.3

- falcosecurity/falco: 9.2.0 -> 9.2.2 (Falco 0.45.0 -> 0.45.2, libs 0.26.1).
  Fixes the modern BPF probe failing to load (`signal_deliver`: invalid mem
  access 'scalar') on kernels that expose siginfo as a scalar, including the
  GitHub runner's 7.0.0-azure kernel (falcosecurity/libs#3119).
- `/proc` is now mounted read-only (upstream chart 9.2.1); the render
  contract enforces it.

## 0.1.2

- CI: raise the memory limit to 1Gi. Falco was OOM-killed at 512Mi on
  startup on the GitHub runner image with kernel 7.0.0-azure.

## 0.1.0

- Vendor falcosecurity/falco chart 9.2.0 (Falco 0.45.0).
- `modern_ebpf` driver in least-privileged mode, no falcoctl downloads,
  CRI-only container metadata.
- JSON alerts to `/var/log/falco/events.json` on each node for
  `charts/wazuh-agent`.
- Chart-owned helm test proving a default-ruleset alert reaches the node file.
