#!/bin/bash
# =============================================================================
# Mock gcloud for bats tests
# =============================================================================
#
# Logs all invocations to $GCLOUD_LOG_FILE and returns configurable responses.
#
# Behavior:
#   - All calls are logged to $GCLOUD_LOG_FILE (one line per invocation)
#   - "describe" and "list" subcommands return 0 by default (resource exists)
#   - All other subcommands print "[MOCK] gcloud $@" and return 0
#   - Set MOCK_GCLOUD_FAIL_DESCRIBE=true to make describe/list return 1
#   - Set MOCK_GCLOUD_FORMAT_VALUE to return a specific value for --format queries

gcloud() {
    local args="$*"

    # Log the call
    if [[ -n "${GCLOUD_LOG_FILE:-}" ]]; then
        echo "gcloud $args" >> "$GCLOUD_LOG_FILE"
    fi

    # Handle describe/list subcommands
    if [[ "$args" == *" describe "* || "$args" == *" list "* ]]; then
        if [[ "${MOCK_GCLOUD_FAIL_DESCRIBE:-false}" == "true" ]]; then
            return 1
        fi
        # Return a format value if requested and configured
        if [[ "$args" == *"--format="* && -n "${MOCK_GCLOUD_FORMAT_VALUE:-}" ]]; then
            echo "$MOCK_GCLOUD_FORMAT_VALUE"
        fi
        return 0
    fi

    echo "[MOCK] gcloud $args"
    return 0
}
export -f gcloud
