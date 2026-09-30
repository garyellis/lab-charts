## 0.1.0

- Vendor twingate/connector chart 0.1.35 (Connector 1.93.0), digest-pinned.
- Tokens come from an existing Secret, never from values.
- Cluster-test `minimal` profile mints a per-cluster connector with
  `scripts/twingate-ci-connector` and releases it at teardown.
