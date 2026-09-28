#!/usr/bin/env bash
set -euo pipefail

# Example cluster-test cleanup hook (chart-lifecycle.yaml, profile minimal).
# Runs at teardown, in reverse install order. Cleanup is best-effort, so a
# real cleanup hook must be idempotent and succeed when nothing is left.
echo "goodbye from ${CHART_MANAGER_HOOK_PHASE:?}: ${CHART_MANAGER_CHART:?}/${CHART_MANAGER_PROFILE:?}" \
  "release=${CHART_MANAGER_RELEASE:?} namespace=${CHART_MANAGER_NAMESPACE:?}" \
  "cluster=${CHART_MANAGER_CLUSTER_NAME:-} context=${CHART_MANAGER_KUBE_CONTEXT:-}"
