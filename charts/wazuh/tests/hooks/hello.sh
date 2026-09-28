#!/usr/bin/env bash
set -euo pipefail

# Example cluster-test preInstall hook (chart-lifecycle.yaml, profile minimal).
# Runs after the namespace exists and before helm upgrade --install; runs again
# on every re-test, so a real preInstall hook must be idempotent.
echo "hello from ${CHART_MANAGER_HOOK_PHASE:?}: ${CHART_MANAGER_CHART:?}/${CHART_MANAGER_PROFILE:?}" \
  "release=${CHART_MANAGER_RELEASE:?} namespace=${CHART_MANAGER_NAMESPACE:?}" \
  "cluster=${CHART_MANAGER_CLUSTER_NAME:-} context=${CHART_MANAGER_KUBE_CONTEXT:-}"
