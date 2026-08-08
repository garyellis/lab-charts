# cosmosdb-emulator

Local-development chart for the Linux-based Azure Cosmos DB emulator.

The emulator image is pinned to the deterministic `vnext-EN20260706` release.
Update both `image.tag` and the chart `appVersion` when adopting a newer release.

The CI profile gives first-time database initialization a 20-minute startup
window. Its lifecycle timeouts are deliberately longer than that window so a
slow GitHub-hosted runner can finish one initialization attempt without the
startup probe restarting the process and repeating initialization work. The
readiness check remains `/ready`; a rollout cannot pass until the emulator
reports that its database is initialized.

```bash
mise run validate -- --all --chart cosmosdb-emulator --env ci
mise run kind-test -- cosmosdb-emulator --profile routed
```

Default Istio hosts:

- Cosmos gateway: `https://cosmos.k8s.home.lab.io`
- Data Explorer: `https://cosmos-explorer.k8s.home.lab.io`

Default in-cluster endpoint:

```text
AccountEndpoint=http://cosmosdb-emulator.azure-dev.svc.cluster.local:8081/;AccountKey=C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw==;
```

The chart defaults the emulator to HTTP mode because the existing apps gateway
terminates TLS and forwards HTTP to backends. Microsoft documents that .NET and
Java SDKs require emulator HTTPS mode; set `emulator.protocol=https` and add the
matching Istio TLS handling if those clients need to connect through the gateway.
