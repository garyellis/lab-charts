# RustFS

This umbrella chart pins `rustfs/rustfs` 0.12.0 (RustFS 1.0.0-beta.12) and
deploys a private, standalone object store for the observability hub. The
default topology is one pod, one 100 GiB data PVC, and no separate log PVC.
RustFS writes logs to container stdout and exposes only a ClusterIP Service.

```mermaid
flowchart LR
  root[External rustfs-root Secret] --> server[RustFS]
  root --> bootstrap[Bootstrap Job]
  workload[External rustfs-thanos Secret] --> bootstrap
  bootstrap --> bucket[(thanos-metrics)]
  bootstrap --> iam[Bucket-scoped service account]
  thanos[Thanos] --> bucket
```

## Secret contract

Create both Secrets in the release namespace before installing the production
values:

| Secret | Key | Consumer |
|---|---|---|
| `rustfs-root` | `RUSTFS_ACCESS_KEY` | RustFS and bootstrap Job |
| `rustfs-root` | `RUSTFS_SECRET_KEY` | RustFS and bootstrap Job |
| `rustfs-thanos` | `accessKey` | bootstrap Job |
| `rustfs-thanos` | `secretKey` | bootstrap Job |

Use non-default, randomly generated credentials. Production credentials must be
managed outside Helm, normally through Flux and SOPS. The separate
`thanos-objstore` Secret supplies the `rustfs-thanos` credentials to Thanos in
its `objstore.yml`; this chart does not create that configuration Secret.

## Tenants

`bootstrap.tenants` maps a tenant name to its buckets, a description, and the
workload Secret that holds its service-account credentials. The production
defaults define only the `thanos` tenant shown above. A bucket may belong to
only one tenant, and the chart fails to render otherwise.

The post-install/post-upgrade bootstrap Job waits for readiness and, for each
tenant, creates any missing buckets and creates or updates a service account
whose policy is limited to that tenant's buckets. On upgrade it reconciles the
policy, description, and secret key for an existing workload access key.

The Helm test runs one pod per tenant. Each pod uses that tenant's workload
credentials to write, read back, verify, and delete a unique object in every
bucket the tenant owns. It calls those S3 APIs directly and does not require
account-wide bucket-listing permission.

## Usage

```shell
helm dependency update charts/rustfs
helm upgrade --install rustfs charts/rustfs \
  --namespace observability \
  --create-namespace \
  -f charts/rustfs/values.yaml
```

The stable in-cluster S3 endpoint is `http://rustfs-svc:9000`. There is no
Ingress, Gateway API route, NodePort, LoadBalancer, or enabled console by
default. Override `rustfs.storageclass.name` where `local-path` is unavailable.

## Validation

```shell
mise run validate -- --chart rustfs --env ci
uv run chart-manager chart test rustfs --profile minimal
```

The CI overlay creates deterministic, non-default local credentials and a
small ephemeral-sized PVC. It also adds `loki` and `mimir` tenants, because the
Loki and Mimir chart tests use this chart as their object store in place of
the MinIO images that are no longer publicly pullable. Those credentials are
test-only and must never be used in a deployed environment.
