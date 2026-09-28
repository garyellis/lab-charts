#!/usr/bin/env bash
set -euo pipefail

# Example cleanup hook: runs at `chart teardown`; must be idempotent.
echo "goodbye from ${CHART_MANAGER_HOOK_PHASE:?}: ${CHART_MANAGER_CHART:?}/${CHART_MANAGER_PROFILE:?}" \
  "release=${CHART_MANAGER_RELEASE:?} namespace=${CHART_MANAGER_NAMESPACE:?}" \
  "cluster=${CHART_MANAGER_CLUSTER_NAME:-} context=${CHART_MANAGER_KUBE_CONTEXT:-}"
