# GitOps Prerequisites Setup

Scripts to install and configure the prerequisites for ArgoCD-managed deployments.

## Scripts

| Script | What it does |
|--------|-------------|
| `install-gitops-operator.sh` | Installs the OpenShift GitOps (ArgoCD) operator from OperatorHub |
| `install-eso-operator.sh` | Installs the External Secrets Operator (ESO) from OperatorHub |
| `setup-gcp-sa.sh` | Creates a GCP service account, grants IAM roles, and creates the bootstrap K8s secret |

All scripts are idempotent — safe to re-run without side effects.

## Quick Start

```bash
# 1. Install operators (cluster-admin required)
bash deploy/gitops/setup/install-gitops-operator.sh
bash deploy/gitops/setup/install-eso-operator.sh

# 2. Set up GCP service account (for Google Cloud target only)
export GOOGLE_CLOUD_PROJECT=my-project-id
bash deploy/gitops/setup/setup-gcp-sa.sh

# 3. Deploy the ArgoCD Applications
oc apply -f deploy/gitops/openshift/application.yaml
oc apply -f deploy/gitops/google-cloud/application.yaml
```

## Manual Alternatives

If you cannot run the scripts (e.g., no cluster-admin access), install the operators manually via the OpenShift web console:

### OpenShift GitOps Operator

1. Open the OpenShift web console
2. Navigate to **Operators > OperatorHub**
3. Search for **"Red Hat OpenShift GitOps"**
4. Click **Install**, select **All namespaces**, and click **Install**
5. Wait for the operator to show **Succeeded** in **Installed Operators**
6. Verify: `oc get argocd -n openshift-gitops`

### External Secrets Operator

1. Open the OpenShift web console
2. Navigate to **Operators > OperatorHub**
3. Search for **"external secrets operator"** (select the Red Hat-supported version)
4. Click **Install**, select the `openshift-external-secrets` namespace, channel `stable-v1`, and click **Install**
5. Wait for the operator to show **Succeeded** in **Installed Operators**
6. Verify: `oc get csv -n openshift-external-secrets`

### GCP Service Account (Manual)

```bash
# Set your project
export PROJECT_ID=my-project-id
export SA_NAME=lightspeed-gitops
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Create service account
gcloud iam service-accounts create ${SA_NAME} \
  --display-name="Lightspeed GitOps Deploy" \
  --project="${PROJECT_ID}"

# Grant IAM roles
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/cloudbuild.builds.editor"

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser"

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.admin"

# Create and download key
gcloud iam service-accounts keys create sa-key.json \
  --iam-account="${SA_EMAIL}" \
  --project="${PROJECT_ID}"

# Create the bootstrap secret on OpenShift
oc create secret generic gcp-sa-bootstrap \
  --from-file=gcp-service-account-key=sa-key.json \
  -n lightspeed-agent-gcp

# Delete the local key file
rm sa-key.json
```

## GCP Service Account Permissions

The GitOps deploy Job service account requires the following IAM roles:

| Role | Purpose |
|------|---------|
| `roles/secretmanager.secretAccessor` | ESO reads secrets from GCP Secret Manager |
| `roles/cloudbuild.builds.editor` | Deploy Job submits Cloud Build pipelines |
| `roles/iam.serviceAccountUser` | Cloud Build impersonates the Cloud Run runtime SA |
| `roles/run.admin` | Cloud Build deploys Cloud Run services |

This service account is separate from the Cloud Run runtime SA created by `deploy/cloudrun/setup.sh`. The runtime SA has additional roles for the running agent (AI Platform, Pub/Sub, Cloud SQL, etc.).

## Environment Variables

### `setup-gcp-sa.sh`

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CLOUD_PROJECT` | (required) | Target GCP project ID |
| `SA_NAME` | `lightspeed-gitops` | GCP service account name |
| `NAMESPACE` | `lightspeed-agent-gcp` | OpenShift namespace for the bootstrap secret |
| `SECRET_NAME` | `gcp-sa-bootstrap` | K8s secret name for the GCP SA key |
| `KEY_FILE` | `/tmp/lightspeed-gitops-sa-key.json` | Path to save the SA key JSON |

### Operator install scripts

| Variable | Default | Description |
|----------|---------|-------------|
| `TIMEOUT` | `300` | Seconds to wait for operator readiness |

## Verification

After running all setup scripts, verify the prerequisites are ready:

```bash
# OpenShift GitOps operator
oc get argocd openshift-gitops -n openshift-gitops -o jsonpath='{.status.phase}'
# Expected: Available

# External Secrets Operator
oc get csv -n openshift-external-secrets -o jsonpath='{.items[0].status.phase}'
# Expected: Succeeded

# GCP bootstrap secret
oc get secret gcp-sa-bootstrap -n lightspeed-agent-gcp
# Expected: secret listed with 1 data key

# ArgoCD UI URL
oc get route openshift-gitops-server -n openshift-gitops -o jsonpath='{.spec.host}'
```
