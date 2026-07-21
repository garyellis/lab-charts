# cosmosdb-emulator

Local-development chart for the Linux-based Azure Cosmos DB emulator.

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
