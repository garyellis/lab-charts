# dynamodb-local

Local-development chart for Amazon DynamoDB Local.

```bash
mise run validate -- --all --chart dynamodb-local --env ci
mise run kind-test -- dynamodb-local --profile routed
```

Default Istio host:

- DynamoDB API: `https://dynamodb.k8s.home.lab.io`

Default in-cluster endpoint:

```text
http://dynamodb-local.aws-dev.svc.cluster.local:8000
```

DynamoDB Local accepts any AWS credentials. The chart stores dummy credentials
and endpoint URLs in `secret/dynamodb-local` for local clients and test pods.
