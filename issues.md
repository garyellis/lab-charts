# Known Issues & Edge Cases

Race conditions and footguns discovered while bringing up the LGTM stack in a kind cluster via `chart-manager kind test`. Each entry: what you'll see, why it happens, and how it's resolved.

---

## 1. loki chunks-cache crashes with `supplied ext_path has zero size`

**Symptom**
```
kubectl logs loki-chunks-cache-0
> supplied ext_path has zero size, cannot use
> failed to parse ext_path argument
```
Pod in `CrashLoopBackOff`. Node has plenty of capacity — this is not a resource issue.

**Root cause**
The loki helm chart computes memcached's extstore file size in `templates/memcached/_memcached-statefulset.tpl:111`:

```
$persistenceSize := (div (mul (trimSuffix "Gi" .persistence.storageSize | trimSuffix "G") 9) 10)
```

That's integer division: `floor(storageSize_in_Gi * 9 / 10)`. With `storageSize: 1Gi` it evaluates to `0G`, and memcached aborts because `ext_path=/data/file:0G` is invalid.

**Fix**
Use `storageSize >= 2Gi` for `chunksCache.persistence`. We use `3Gi` in `charts/loki/values-ci.yaml`.

---

## 2. `helm --wait` deadlocks against post-install hooks that bootstrap object storage

**Symptom**
- `helm status loki` shows `pending-install`
- Install eventually times out after `profile.timeout` (15m)
- `loki-0` keeps crashing with `NoSuchBucket: The specified bucket does not exist`
- `kubectl get jobs` shows no `loki-minio-post-job` (even though `helm template` renders one)

**Root cause**
Helm's install lifecycle with `--wait`:

1. Apply main resources
2. **Wait** for them to be Ready
3. Run `post-install` hooks
4. Mark release `deployed`

The loki subchart creates its minio buckets via a Job annotated `helm.sh/hook: post-install`. That hook can only fire at step 3. But step 2 never completes, because `loki-0` cannot be Ready without the `chunks` bucket — which is exactly what the step-3 hook would create.

Result: circular wait, helm times out, the bucket Job is never actually created in the cluster.

**Why this works elsewhere**
Almost no downstream consumer of this chart uses `--wait`:
- Plain `helm install` — no `--wait` by default
- helmfile — doesn't pass `--wait` unless explicitly configured per release
- Argo CD / Flux — don't use Helm's hook lifecycle; apply manifests directly with their own ordering

Without `--wait`, helm applies manifests, immediately fires post-install hooks, the bucket Job runs, and loki recovers on its own restart loop within ~30s.

**Fix in chart-manager**
`services/kind_test.py` calls `upgrade_install(..., wait=False)`. The same bootstrap pattern exists in `mimir-distributed` and any other chart that ships a bundled minio + bucket-creation hook, so this is the right default for kind-based smoke tests.

---

## 3. `helm test` race when install runs with `wait=False`

**Symptom**
First-run failure with `Phase: Failed`:
```
helm test failed for loki: ... pod loki-helm-test failed
```
Manually re-running `helm test loki` seconds later succeeds.

**Root cause**
Solving issue #2 by dropping `--wait` means `helm upgrade --install` returns in seconds — before the release's pods are actually Ready. `helm test` is invoked immediately after install and runs its test pod against a release that's still warming up (still waiting for its bucket Job, still rolling out, etc.).

**Fix in chart-manager**
Between install and test, `services/kind_test.py` calls `Kubectl.wait_workloads_ready(namespace, timeout)` (`integrations/kubectl.py`), which runs `kubectl rollout status` for every `deployment`, `statefulset`, and `daemonset` in the namespace.

`rollout status` is used (not `kubectl wait --for=condition=Ready pod --all`) because pods belonging to completed Jobs — like the minio bucket-creation job — never reach `Ready=True` (they're `Succeeded`), and a blanket pod wait would hang on them. `rollout status` operates on the controllers, not the pods, so it cleanly gates on long-lived workloads only.

---

## 4. `helm test --logs` races against `hook-delete-policy: hook-succeeded`

**Symptom**
Test passes, but the wrapper still raises:
```
TEST SUITE: mimir-distributed-smoke-test
Phase:      Succeeded
Error: unable to get pod logs for mimir-distributed-smoke-test: pods "mimir-distributed-smoke-test" not found
```
`helm test` exits non-zero even though the test itself succeeded.

**Root cause**
Many chart test pods are annotated `helm.sh/hook-delete-policy: hook-succeeded`, so Kubernetes deletes the test pod the moment it exits 0. Helm's `--logs` flag fetches the pod's stdout *after* the test completes — by which time the delete may already have happened. Log fetch fails, helm surfaces it as a top-level error and exits non-zero, even though the test it actually ran passed.

`--logs` is only useful on test *failures* (where pods are typically retained unless `hook-failed` is also in the delete policy), but it breaks success cases unconditionally. Net-negative.

**Fix in chart-manager**
`Helm.test` in `integrations/helm.py` no longer passes `--logs`. On test failure, `services/kind_test.py` already calls `Kubectl.diagnostics(namespace)` which dumps pods + events — enough to start debugging.

For deeper investigation when a test fails, run manually:
```sh
helm test <release> -n <ns> --logs
```
The failing pod is usually still around (unless the chart's hook-delete-policy is `before-hook-creation,hook-succeeded,hook-failed`).

---

## 5. Umbrella chart values: subchart keys must be nested under `loki:`

**Symptom**
Settings declared in `charts/loki/values.yaml` appear to have no effect — the running release uses subchart defaults instead.

**Root cause**
`charts/loki/` is an umbrella chart whose `Chart.yaml` declares the upstream `grafana/loki` chart as a subchart named `loki`. Helm only forwards values to a subchart when they're nested under that subchart's name at the top level.

Top-level keys like `chunksCache:`, `minio:`, `singleBinary:`, `gateway:`, `ingester:`, etc. in `values.yaml` are silently ignored by the loki subchart. The CI overlay `values-ci.yaml` does this correctly (everything wrapped under a top-level `loki:` key).

**Fix**
Re-shape `charts/loki/values.yaml` so all subchart-targeted blocks live under `loki:`, matching the layout of `values-ci.yaml`. Until that lands, only the `loki: { auth_enabled, commonConfig, ... }` block in `values.yaml` actually reaches the subchart; everything else relies on subchart defaults.
