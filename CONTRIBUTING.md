# Contributing

## Handling Renovate CVE Alerts

This repository uses [Renovate](https://docs.renovatebot.com/) via Konflux MintMaker with OSV vulnerability alerting enabled. When a known CVE affects a Python dependency — direct or transitive — Renovate automatically opens an alert PR against `main` from the `red-hat-konflux` bot.

These alert PRs are **informational only**. Renovate scans the `requirements-*.txt` lock files, which contain the full dependency tree (both direct and transitive dependencies), and detects known CVEs via the OSV database. Each alert PR includes details about the CVE and the required version fix. However, Renovate does not support lock file regeneration with `uv`, which this project uses to produce hashed lock files (`make lock`). Because of this, contributors must manually apply the fix following the steps below.

## Fixing a CVE

When a Renovate CVE alert PR appears, create a **dedicated PR** to fix it. Do not bundle CVE fixes into unrelated feature or chore PRs.

### Steps

1. **Create a branch** for the fix (e.g., `fix/<package>-cve` or `fix/cve-<id>`).

2. **Update `pyproject.toml`:**
   - **Direct dependency** — bump the version constraint to the patched version.
   - **Transitive dependency** — add it as a direct dependency with the safe version constraint. Lock files cannot be edited manually.

   ```toml
   # Example: constraining a transitive dependency to fix a CVE
   dependencies = [
       "pydantic-core>=2.41.6",  # CVE fix: force safe version
   ]
   ```

3. **Set up the virtual environment** (if not already done):

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[agent,dev]"
   ```

4. **Regenerate lock files:**

   ```bash
   make lock
   ```

5. **Verify the fix:**

   ```bash
   make audit            # Confirm the CVE no longer appears
   make lint && make test # Confirm nothing is broken
   ```

6. **Commit both `pyproject.toml` and the `requirements-*.txt` lock files together** in one commit.

7. **Open a PR** targeting `main` with the fix. Reference the CVE ID and the Renovate alert PR in the description.

8. **Close the Renovate alert PR** once the fix is merged. Reference your fix PR in a comment before closing so there is a clear audit trail.

### Why Dedicated PRs

CVE fixes should be isolated in their own PRs so they:
- Can be reviewed and merged quickly without waiting on unrelated work
- Do not block other PRs that have no dependency changes
- Are easy to revert if a patched version introduces a regression

## Shell Tests for `deploy.sh`

Changes to `deploy/cloudrun/deploy.sh` must include corresponding test updates in `tests/shell/`. CI only validates that existing tests pass — it does not detect missing coverage for new code paths. If you add or rename a function in `deploy.sh`, add tests that exercise it in `tests/shell/deploy.bats`.

### What to test

- **New functions**: add at least one `@test` block that calls the function and verifies its behavior (arguments, gcloud commands invoked, exit codes).
- **Renamed functions**: update existing test references to match the new name.
- **New flags or env vars**: add argument-parsing and validation tests.
- **New code paths in existing functions**: add a test case covering the new branch (e.g., a new conditional, error path, or feature toggle).

### Running shell tests locally

```bash
sudo dnf install -y bats shellcheck   # or: sudo apt-get install -y bats shellcheck
make test-shell                        # runs: bats tests/shell/
shellcheck deploy/cloudrun/*.sh        # lint shell scripts
```

The test suite uses a mock `gcloud` (`tests/shell/mock_gcloud.sh`) that logs invocations instead of calling the real CLI. See existing tests in `deploy.bats` for patterns.

## CI Pipeline

GitHub Actions (`.github/workflows/ci.yml`) runs on CentOS Stream 9 with Python 3.12:

1. **Konflux Verify** — validates GPG signatures and Signed-off-by trailers on Konflux bot PRs
2. **Detect Changes** — path-based filter that gates downstream jobs (python, pyproject changes)
3. **Lock File Verification** — ensures lock files are in sync with pyproject.toml (runs when pyproject.toml changes)
4. **Vulnerability Scan** — pip-audit for known CVEs
5. **Lint** — ruff + mypy
6. **Test** — pytest
7. **Build** — Podman container build
8. **Container Scan** — Trivy vulnerability scan on built container images
9. **Shell Tests** — shellcheck linting + bats tests + function coverage check for `deploy.sh`
10. **CI Gate** — blocks merge if any job fails

Secret scanning is configured via `.gitleaks.toml`. CVE alerting for Python dependencies is managed via Renovate (`renovate.json`).
