#!/bin/bash
# =============================================================================
# Google Cloud Run Deployment Cleanup Script
# =============================================================================
#
# This script removes all GCP resources created by setup.sh and deploy.sh:
# - Cloud Run services
# - Pub/Sub topic and subscription
# - Secrets in Secret Manager
# - Service accounts (runtime + Pub/Sub invoker) and IAM bindings
#
# Usage:
#   ./deploy/cloudrun/cleanup.sh [--force]
#
# Options:
#   --force    Skip confirmation prompt
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - GOOGLE_CLOUD_PROJECT environment variable set
#
# =============================================================================

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-lightspeed-agent}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-${SERVICE_NAME}}"
HANDLER_SERVICE_NAME="${HANDLER_SERVICE_NAME:-marketplace-handler}"
DB_INSTANCE_NAME="${DB_INSTANCE_NAME:-lightspeed-agent-db}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Pub/Sub Invoker Service Account (must match setup.sh)
PUBSUB_INVOKER_NAME="${PUBSUB_INVOKER_NAME:-pubsub-invoker}"
PUBSUB_INVOKER_SA="${PUBSUB_INVOKER_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Pub/Sub configuration
PUBSUB_TOPIC="${PUBSUB_TOPIC:-marketplace-entitlements}"
PUBSUB_SUBSCRIPTION="${PUBSUB_SUBSCRIPTION:-${PUBSUB_TOPIC}-sub}"

# Per-service load balancer configuration
ENABLE_LB_AGENT="${ENABLE_LB_AGENT:-false}"
ENABLE_LB_HANDLER="${ENABLE_LB_HANDLER:-false}"
ENABLE_CLOUD_ARMOR_AGENT="${ENABLE_CLOUD_ARMOR_AGENT:-false}"
ENABLE_CLOUD_ARMOR_HANDLER="${ENABLE_CLOUD_ARMOR_HANDLER:-false}"
LB_NAME="${LB_NAME:-lightspeed-lb}"

# Parse arguments
FORCE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --force)
            FORCE=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Usage: $0 [--force]"
            exit 1
            ;;
    esac
done

# Validate required variables
if [[ -z "$PROJECT_ID" ]]; then
    log_error "GOOGLE_CLOUD_PROJECT environment variable is required"
    echo "  export GOOGLE_CLOUD_PROJECT=your-project-id"
    exit 1
fi

log_warn "This will delete the following resources from project: $PROJECT_ID"
echo ""
echo "  - Cloud Run services: $SERVICE_NAME, $HANDLER_SERVICE_NAME"
if [[ "$ENABLE_LB_AGENT" == "true" ]]; then
    echo "  - Agent LB: forwarding rule, HTTPS proxy, URL map, SSL cert,"
    echo "              backend service, NEG, static IP (prefix: ${LB_NAME}-agent)"
fi
if [[ "$ENABLE_CLOUD_ARMOR_AGENT" == "true" ]]; then
    echo "  - Agent Cloud Armor: security policy and WAF rules (${LB_NAME}-agent-security-policy)"
fi
if [[ "$ENABLE_LB_HANDLER" == "true" ]]; then
    echo "  - Handler LB: forwarding rule, HTTPS proxy, URL map, SSL cert,"
    echo "                backend service, NEG, static IP (prefix: ${LB_NAME}-handler)"
fi
if [[ "$ENABLE_CLOUD_ARMOR_HANDLER" == "true" ]]; then
    echo "  - Handler Cloud Armor: security policy and WAF rules (${LB_NAME}-handler-security-policy)"
fi
echo "  - Pub/Sub topic: $PUBSUB_TOPIC"
echo "  - Pub/Sub subscription: $PUBSUB_SUBSCRIPTION"
echo "  - Secrets: redhat-sso-client-id, redhat-sso-client-secret, database-url,"
echo "             session-database-url, gma-client-id, gma-client-secret, dcr-encryption-key,"
echo "             rate-limit-redis-url"
echo "  - Service accounts: $SERVICE_ACCOUNT"
echo "                      $PUBSUB_INVOKER_SA"
echo ""

# Confirmation prompt
if [[ "$FORCE" != "true" ]]; then
    read -p "Are you sure you want to delete these resources? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Cleanup cancelled"
        exit 0
    fi
fi

echo ""
log_info "Starting cleanup..."

# =============================================================================
# Step 1: Delete Cloud Run Services
# =============================================================================
log_info "Deleting Cloud Run services..."

# Delete lightspeed-agent service
if gcloud run services describe "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud run services delete "$SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Cloud Run service '$SERVICE_NAME' deleted"
else
    log_info "Cloud Run service '$SERVICE_NAME' does not exist, skipping"
fi

# Delete marketplace-handler service
if gcloud run services describe "$HANDLER_SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud run services delete "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Cloud Run service '$HANDLER_SERVICE_NAME' deleted"
else
    log_info "Cloud Run service '$HANDLER_SERVICE_NAME' does not exist, skipping"
fi

# =============================================================================
# Step 2: Delete Load Balancer Resources (per-service)
# =============================================================================
# Reusable function to clean up all LB resources for a single service.
# Usage: cleanup_service_lb <service_label>
cleanup_service_lb() {
    local service_label="$1"

    local rule_name="${LB_NAME}-${service_label}-forwarding-rule"
    local proxy_name="${LB_NAME}-${service_label}-https-proxy"
    local url_map_name="${LB_NAME}-${service_label}-url-map"
    local cert_name="${LB_NAME}-${service_label}-cert"
    local policy_name="${LB_NAME}-${service_label}-security-policy"
    local backend_name="${LB_NAME}-${service_label}-backend"
    local neg_name="${LB_NAME}-${service_label}-neg"
    local ip_name="${LB_NAME}-${service_label}-ip"

    log_info "Deleting ${service_label} load balancer resources (reverse order)..."

    # Delete forwarding rule
    if gcloud compute forwarding-rules describe "$rule_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute forwarding-rules delete "$rule_name" \
            --global --project="$PROJECT_ID" --quiet
        log_info "Forwarding rule '$rule_name' deleted"
    else
        log_info "Forwarding rule '$rule_name' does not exist, skipping"
    fi

    # Delete HTTPS proxy
    if gcloud compute target-https-proxies describe "$proxy_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute target-https-proxies delete "$proxy_name" \
            --global --project="$PROJECT_ID" --quiet
        log_info "HTTPS proxy '$proxy_name' deleted"
    else
        log_info "HTTPS proxy '$proxy_name' does not exist, skipping"
    fi

    # Delete URL map
    if gcloud compute url-maps describe "$url_map_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute url-maps delete "$url_map_name" \
            --global --project="$PROJECT_ID" --quiet
        log_info "URL map '$url_map_name' deleted"
    else
        log_info "URL map '$url_map_name' does not exist, skipping"
    fi

    # Delete SSL certificate
    if gcloud compute ssl-certificates describe "$cert_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute ssl-certificates delete "$cert_name" \
            --global --project="$PROJECT_ID" --quiet
        log_info "SSL certificate '$cert_name' deleted"
    else
        log_info "SSL certificate '$cert_name' does not exist, skipping"
    fi

    # Detach any security policy from the backend before deleting it.
    # Always attempt this regardless of the ENABLE_CLOUD_ARMOR_* flag — the
    # user may have deployed with Cloud Armor but forgotten to set the flag
    # during cleanup.
    gcloud compute backend-services update "$backend_name" \
        --security-policy="" --global --project="$PROJECT_ID" 2>/dev/null || true

    # Delete the Cloud Armor security policy if it exists
    if gcloud compute security-policies describe "$policy_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute security-policies delete "$policy_name" \
            --global --project="$PROJECT_ID" --quiet
        log_info "Security policy '$policy_name' deleted"
    fi

    # Delete backend service
    if gcloud compute backend-services describe "$backend_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute backend-services delete "$backend_name" \
            --global --project="$PROJECT_ID" --quiet
        log_info "Backend service '$backend_name' deleted"
    else
        log_info "Backend service '$backend_name' does not exist, skipping"
    fi

    # Delete serverless NEG
    if gcloud compute network-endpoint-groups describe "$neg_name" \
        --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute network-endpoint-groups delete "$neg_name" \
            --region="$REGION" --project="$PROJECT_ID" --quiet
        log_info "NEG '$neg_name' deleted"
    else
        log_info "NEG '$neg_name' does not exist, skipping"
    fi

    # Delete static IP address
    if gcloud compute addresses describe "$ip_name" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute addresses delete "$ip_name" \
            --global --project="$PROJECT_ID" --quiet
        log_info "Static IP '$ip_name' deleted"
    else
        log_info "Static IP '$ip_name' does not exist, skipping"
    fi
}

if [[ "$ENABLE_LB_AGENT" == "true" ]]; then
    cleanup_service_lb "agent"
fi

if [[ "$ENABLE_LB_HANDLER" == "true" ]]; then
    cleanup_service_lb "handler"
fi

if [[ "$ENABLE_LB_AGENT" != "true" && "$ENABLE_LB_HANDLER" != "true" ]]; then
    log_info "Skipping load balancer cleanup (no per-service LBs enabled)"
fi

# =============================================================================
# Step 3: Delete Pub/Sub Resources
# =============================================================================
log_info "Deleting Pub/Sub resources..."

# Delete subscription first (must be deleted before topic)
if gcloud pubsub subscriptions describe "$PUBSUB_SUBSCRIPTION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud pubsub subscriptions delete "$PUBSUB_SUBSCRIPTION" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Pub/Sub subscription '$PUBSUB_SUBSCRIPTION' deleted"
else
    log_info "Pub/Sub subscription '$PUBSUB_SUBSCRIPTION' does not exist, skipping"
fi

# Delete topic (skip if cross-project — the topic is managed externally)
if [[ "$PUBSUB_TOPIC" == projects/* ]]; then
    log_info "Pub/Sub topic is a cross-project reference, skipping deletion: $PUBSUB_TOPIC"
elif gcloud pubsub topics describe "$PUBSUB_TOPIC" --project="$PROJECT_ID" &>/dev/null; then
    gcloud pubsub topics delete "$PUBSUB_TOPIC" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Pub/Sub topic '$PUBSUB_TOPIC' deleted"
else
    log_info "Pub/Sub topic '$PUBSUB_TOPIC' does not exist, skipping"
fi

# =============================================================================
# Step 4: Delete Secrets
# =============================================================================
log_info "Deleting secrets from Secret Manager..."

secrets=(
    "redhat-sso-client-id"
    "redhat-sso-client-secret"
    "database-url"
    "session-database-url"
    "gma-client-id"
    "gma-client-secret"
    "dcr-encryption-key"
    "rate-limit-redis-url"
)

for secret in "${secrets[@]}"; do
    if gcloud secrets describe "$secret" --project="$PROJECT_ID" &>/dev/null; then
        gcloud secrets delete "$secret" \
            --project="$PROJECT_ID" \
            --quiet
        log_info "  Secret '$secret' deleted"
    else
        log_info "  Secret '$secret' does not exist, skipping"
    fi
done

# =============================================================================
# Step 5: Remove IAM Bindings and Delete Service Account
# =============================================================================
log_info "Removing service account IAM bindings..."

roles=(
    "roles/secretmanager.secretAccessor"
    "roles/aiplatform.user"
    "roles/pubsub.subscriber"
    "roles/pubsub.publisher"
    "roles/servicemanagement.serviceController"
    "roles/logging.logWriter"
    "roles/monitoring.metricWriter"
    "roles/cloudsql.client"
    "roles/serviceusage.serviceUsageConsumer"
)

if gcloud iam service-accounts describe "$SERVICE_ACCOUNT" --project="$PROJECT_ID" &>/dev/null; then
    for role in "${roles[@]}"; do
        log_info "  Removing $role..."
        gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
            --member="serviceAccount:$SERVICE_ACCOUNT" \
            --role="$role" \
            --quiet 2>/dev/null || true
    done

    log_info "Deleting runtime service account..."
    gcloud iam service-accounts delete "$SERVICE_ACCOUNT" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Service account '$SERVICE_ACCOUNT' deleted"
else
    log_info "Service account '$SERVICE_ACCOUNT' does not exist, skipping"
fi

# Delete Pub/Sub Invoker Service Account
log_info "Removing Pub/Sub Invoker service account..."

if gcloud iam service-accounts describe "$PUBSUB_INVOKER_SA" --project="$PROJECT_ID" &>/dev/null; then
    # Remove the service-level run.invoker binding on marketplace-handler
    log_info "  Removing roles/run.invoker from $HANDLER_SERVICE_NAME..."
    gcloud run services remove-iam-policy-binding "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/run.invoker" \
        --quiet 2>/dev/null || true

    # Remove the self-referencing serviceAccountUser binding
    log_info "  Removing roles/iam.serviceAccountUser..."
    gcloud iam service-accounts remove-iam-policy-binding "$PUBSUB_INVOKER_SA" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/iam.serviceAccountUser" \
        --project="$PROJECT_ID" \
        --quiet 2>/dev/null || true

    # Remove the project-level pubsub.editor binding
    log_info "  Removing roles/pubsub.editor..."
    gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/pubsub.editor" \
        --quiet 2>/dev/null || true

    log_info "Deleting Pub/Sub Invoker service account..."
    gcloud iam service-accounts delete "$PUBSUB_INVOKER_SA" \
        --project="$PROJECT_ID" \
        --quiet
    log_info "Service account '$PUBSUB_INVOKER_SA' deleted"
else
    log_info "Service account '$PUBSUB_INVOKER_SA' does not exist, skipping"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
log_info "=========================================="
log_info "Cleanup complete!"
log_info "=========================================="
echo ""
echo "The following resources have been removed:"
echo "  - Cloud Run services ($SERVICE_NAME, $HANDLER_SERVICE_NAME)"
if [[ "$ENABLE_LB_AGENT" == "true" ]]; then
    echo "  - Agent LB resources (forwarding rule, proxy, URL map, cert, backend, NEG, IP)"
fi
if [[ "$ENABLE_CLOUD_ARMOR_AGENT" == "true" ]]; then
    echo "  - Agent Cloud Armor security policy and WAF rules"
fi
if [[ "$ENABLE_LB_HANDLER" == "true" ]]; then
    echo "  - Handler LB resources (forwarding rule, proxy, URL map, cert, backend, NEG, IP)"
fi
if [[ "$ENABLE_CLOUD_ARMOR_HANDLER" == "true" ]]; then
    echo "  - Handler Cloud Armor security policy and WAF rules"
fi
echo "  - Pub/Sub topic and subscription"
echo "  - Secret Manager secrets"
echo "  - Service accounts (runtime + Pub/Sub invoker) and IAM bindings"
echo ""
echo "Note: The following resources were NOT deleted (delete manually if needed):"
echo "  - Cloud SQL instances"
echo "  - Cloud Memorystore Redis instances"
echo "  - Container images in GCR/Artifact Registry"
echo "  - VPC connectors"
echo "  - Cloud Build triggers"
echo ""
echo "To delete these, use the respective gcloud commands:"
echo "  gcloud sql instances delete $DB_INSTANCE_NAME --project=$PROJECT_ID"
echo "  gcloud redis instances delete lightspeed-redis --region=$REGION --project=$PROJECT_ID"
echo "  gcloud container images delete gcr.io/$PROJECT_ID/$SERVICE_NAME --force-delete-tags --quiet"
echo "  gcloud container images delete gcr.io/$PROJECT_ID/$HANDLER_SERVICE_NAME --force-delete-tags --quiet"
echo "  gcloud container images delete gcr.io/$PROJECT_ID/red-hat-lightspeed-mcp --force-delete-tags --quiet"
echo "  gcloud compute networks vpc-access connectors delete lightspeed-redis-conn --region=$REGION --project=$PROJECT_ID"
echo ""
