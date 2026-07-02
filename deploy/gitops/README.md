# GitOps Deployment

ArgoCD-managed deployments for the Red Hat Lightspeed Agent. This directory contains configuration for two deployment targets:

- **OpenShift** (`openshift/`) — ArgoCD Application that syncs the existing `deploy/openshift/` Helm chart with environment-specific overrides
- **Google Cloud** (`google-cloud/`) — Helm chart where ArgoCD sync hooks trigger Cloud Build to deploy Cloud Run services

## Directory Structure

```
deploy/gitops/
├── README.md
├── setup/                       # Prerequisite installation scripts
│   ├── README.md                # Setup instructions and manual alternatives
│   ├── install-gitops-operator.sh
│   ├── install-eso-operator.sh
│   └── setup-gcp-sa.sh
├── openshift/
│   ├── application.yaml        # ArgoCD Application CR (multi-source)
│   └── values-override.yaml    # Environment-specific Helm value overrides
└── google-cloud/
    ├── Chart.yaml
    ├── application.yaml         # ArgoCD Application CR (multi-source)
    ├── values.yaml              # All parameters with defaults
    ├── values-override.yaml     # Example environment overrides
    ├── secrets.yaml.example     # Bootstrap secret setup instructions
    └── templates/
        ├── _helpers.tpl             # Helm template helpers
        ├── deployment-config.yaml   # ConfigMap with Cloud Build substitutions
        ├── secret-store.yaml        # ESO SecretStore for GCP Secret Manager
        ├── external-secret.yaml     # ESO ExternalSecret for GCP SA key
        ├── serviceaccount.yaml      # SA + RBAC for Jobs
        ├── deploy-job.yaml          # PostSync: submit Cloud Build pipeline
        └── NOTES.txt                # Post-install notes
```

## Prerequisites

- OpenShift cluster with ArgoCD (OpenShift GitOps operator) installed
- External Secrets Operator installed (OperatorHub > "external-secrets-operator") — for the Google Cloud target
- For Google Cloud target: a GCP service account key with Cloud Build, Secret Manager, and Cloud Run permissions

Setup scripts are provided in `setup/` to automate these prerequisites. See [`setup/README.md`](setup/README.md) for details, environment variables, and manual alternatives.

## OpenShift Target

The OpenShift target uses ArgoCD to sync the existing Helm chart at `deploy/openshift/` with GitOps-managed overrides.

### Setup

1. Edit `openshift/values-override.yaml` with your environment values (image tags, deployment mode, provider URL).

2. Apply the ArgoCD Application:
   ```bash
   oc apply -f deploy/gitops/openshift/application.yaml
   ```

3. ArgoCD will automatically sync the chart. The Application uses multi-source to safely reference the override file from a separate directory, and is configured with:
   - **Automated sync** with self-heal and pruning
   - **Server-side apply** for conflict-free updates
   - **Retry** (3 attempts with exponential backoff)

### Updating

Change image tags or configuration in `openshift/values-override.yaml`, commit, and push. ArgoCD syncs automatically on merge.

## Google Cloud Target

The Google Cloud target deploys Cloud Run services through Cloud Build. ArgoCD manages Kubernetes resources on OpenShift (ConfigMap, ExternalSecret, Jobs), and the PostSync hook Job calls out to GCP.

### How It Works

1. A PR changes image tags or config in `google-cloud/values-override.yaml`
2. PR merges to the tracked branch
3. ArgoCD detects the change and begins sync
4. ESO's **ExternalSecret** pulls the GCP SA key from GCP Secret Manager into a K8s Secret
5. **PostSync** — `deploy-job` clones the repo and runs `gcloud builds submit` with substitutions from the ConfigMap
6. Cloud Build pulls images from Quay.io, scans them, pushes to GCR, and deploys Cloud Run services

### Setup

1. Run `setup.sh` once to create all GCP resources (APIs, service accounts, secrets in GCP Secret Manager, Cloud SQL, Redis, Pub/Sub):
   ```bash
   ./deploy/cloudrun/setup.sh
   ```

2. Create the bootstrap secret for ESO authentication:
   ```bash
   oc create secret generic gcp-sa-bootstrap \
     --from-file=gcp-service-account-key=sa-key.json \
     -n lightspeed-agent-gcp
   ```

3. Edit `google-cloud/values-override.yaml` with your GCP project ID and image tags.

4. Apply the ArgoCD Application:
   ```bash
   oc apply -f deploy/gitops/google-cloud/application.yaml
   ```

5. (Optional) For private git repositories, create a token secret:
   ```bash
   oc create secret generic git-credentials \
     --from-literal=token=<GITHUB_PAT> \
     -n lightspeed-agent-gcp
   ```
   Then set `deploy.gitTokenSecret: git-credentials` in your values override.

### Updating

To deploy new image versions, update the tags in your values override file:

```yaml
images:
  agent:
    tag: v1.2.3
  handler:
    tag: v1.2.3
```

Commit and push. ArgoCD syncs the ConfigMap change, then the PostSync Job triggers Cloud Build with the new substitutions.

### Secrets Management

Secrets are managed using the **External Secrets Operator** (ESO), Red Hat's supported approach for GitOps secrets management on OpenShift.

**How it works:**
1. Application secrets (SSO credentials, database URLs, etc.) are stored in **GCP Secret Manager** — created by `setup.sh` or managed directly
2. A **SecretStore** CR configures ESO to connect to GCP Secret Manager using a bootstrap GCP SA key
3. An **ExternalSecret** CR tells ESO which GCP SM secrets to pull into K8s Secrets
4. ESO automatically refreshes secrets based on `externalSecrets.refreshInterval` (default: 1 hour)
5. Cloud Run services read directly from GCP Secret Manager — no need to replicate all secrets to K8s

**Bootstrap credential:** The GCP SA key is the only secret managed manually. Create it once per cluster as described in setup step 2.

To disable ESO (use manually-created K8s Secrets instead):
```yaml
externalSecrets:
  enabled: false
```

### Cloud Build Substitutions

The ConfigMap maps `values.yaml` fields to Cloud Build `_VARIABLE` names. All substitutions match `cloudbuild.yaml` in the repository root. Key mappings:

| values.yaml | Cloud Build Variable |
|---|---|
| `project.id` | `GOOGLE_CLOUD_PROJECT` |
| `project.region` | `_REGION` |
| `images.agent.tag` | `_IMAGE_TAG` |
| `images.agent.source` | `_AGENT_SOURCE_IMAGE` |
| `services.agent.name` | `_SERVICE_NAME` |
| `loadBalancer.agent.enabled` | `_ENABLE_LB_AGENT` |
| `security.scanSeverity` | `_SCAN_SEVERITY` |

See `templates/deployment-config.yaml` for the complete mapping.

### GCP Service Account Roles

The GCP service account used for the deploy Job needs the following IAM roles:

| Role | Purpose |
|---|---|
| `roles/secretmanager.secretAccessor` | ESO reads secrets from GCP SM |
| `roles/cloudbuild.builds.editor` | Submit Cloud Build pipelines |
| `roles/iam.serviceAccountUser` | Impersonate the Cloud Run runtime SA |
| `roles/run.admin` | Deploy Cloud Run services (via Cloud Build) |
