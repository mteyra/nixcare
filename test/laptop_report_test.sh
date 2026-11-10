#!/usr/bin/env bash
# test/laptop_report_test.sh - Invariant tests for laptop-report / telemetry
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "================================================================="
echo " RUNNING NIXCARE LAPTOP-REPORT INVARIANT TEST SUITE"
echo " Invariants tested:"
echo "   1. tracked <= window"
echo "   2. sum(sites per browser) <= that browser's focus time"
echo "   3. sum(apps) <= active time <= tracked"
echo "================================================================="

export PYTHONPATH="${PROJECT_DIR}/modules:${PYTHONPATH:-}"

python3 -m unittest discover -s "${SCRIPT_DIR}" -p "*test*.py" -v

echo "================================================================="
echo " ALL INVARIANT TESTS PASSED SUCCESSFULLY!"
echo "================================================================="
