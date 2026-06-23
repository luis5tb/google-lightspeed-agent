#!/usr/bin/env bash
# Verify that every public function in deploy.sh is referenced in bats tests.
# Exits 1 if any function has zero test references.

set -euo pipefail

DEPLOY_SCRIPT="deploy/cloudrun/deploy.sh"
TEST_DIR="tests/shell"

SKIP_FUNCTIONS="log_info|log_warn|log_error"

functions=$(grep -oP '^[a-z_]+(?=\(\))' "$DEPLOY_SCRIPT" | grep -Ev "^(${SKIP_FUNCTIONS})$")

missing=()
for func in $functions; do
    if ! grep -rq "$func" "$TEST_DIR"/*.bats 2>/dev/null; then
        missing+=("$func")
    fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: The following deploy.sh functions have no test references in $TEST_DIR/:"
    for func in "${missing[@]}"; do
        echo "  - $func"
    done
    echo ""
    echo "Add tests for these functions in $TEST_DIR/deploy.bats."
    echo "See CONTRIBUTING.md for guidelines."
    exit 1
fi

echo "OK: All deploy.sh functions are referenced in tests."
