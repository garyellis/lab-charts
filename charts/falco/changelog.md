## 0.1.0

- Vendor falcosecurity/falco chart 9.2.0 (Falco 0.45.0).
- `modern_ebpf` driver in least-privileged mode, no falcoctl downloads,
  CRI-only container metadata.
- JSON alerts to `/var/log/falco/events.json` on each node for
  `charts/wazuh-agent`.
- Chart-owned helm test proving a default-ruleset alert reaches the node file.
