#!/usr/bin/env bash
set -euo pipefail

# Entry point for scripts/check-chart-source-contracts (CI). The contract
# itself lives in render-contract.sh so it can also be run by name.
chart_dir="${1:-charts/wazuh-agent}"
exec "$(dirname "${BASH_SOURCE[0]}")/render-contract.sh" "${chart_dir}"
