#!/bin/bash
# =============================================================================
# Google Cloud Run Deployment Script
# =============================================================================
#
# Deploys BOTH services to Google Cloud Run:
# 1. marketplace-handler - Handles DCR and Pub/Sub events (always running)
# 2. lightspeed-agent - A2A agent with MCP sidecar (runs after provisioning)
#
# Uses the YAML service configs (service.yaml and marketplace-handler.yaml)
# with variable substitution to deploy each service.
#
# Usage:
#   ./deploy/cloudrun/deploy.sh [OPTIONS]
#
# Options:
#   --service <service>       Which service to deploy: all, handler, agent
#                             (default: all)
#   --image <image>           Container image for the agent
#                             (default: gcr.io/$PROJECT_ID/lightspeed-agent:latest)
#   --handler-image <image>   Container image for the marketplace handler
#                             (default: gcr.io/$PROJECT_ID/marketplace-handler:latest)
#   --mcp-image <image>       Container image for the MCP server
#                             (default: gcr.io/$PROJECT_ID/red-hat-lightspeed-mcp:latest)
#   --allow-unauthenticated   Allow public access
#   --build                   Build images before deploying
#
# Architecture:
#   ┌─────────────────────────┐     ┌─────────────────────────┐
#   │  Marketplace Handler    │     │   Lightspeed Agent      │
#   │  (Cloud Run #1)         │     │    (Cloud Run #2)       │
#   │                         │     │                         │
#   │  - POST /dcr            │     │  - POST / (A2A)         │
#   │  - Pub/Sub push         │     │  - /.well-known/agent   │
#   │  - Account approval     │     │  - OAuth flow           │
#   │  - GMA SSO API          │     │  - MCP sidecar          │
#   └─────────────────────────┘     └─────────────────────────┘
#
# Prerequisites:
#   - Run setup.sh first to configure GCP services
#   - Update secrets in Secret Manager with actual values
#
# =============================================================================

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# =============================================================================
# Configuration
# =============================================================================

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
VERTEXAI_LOCATION="${VERTEXAI_LOCATION:-global}"
SERVICE_NAME="${SERVICE_NAME:-lightspeed-agent}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-${SERVICE_NAME}}"
HANDLER_SERVICE_NAME="${HANDLER_SERVICE_NAME:-marketplace-handler}"
DB_INSTANCE_NAME="${DB_INSTANCE_NAME:-lightspeed-agent-db}"
VPC_CONNECTOR_NAME="${VPC_CONNECTOR_NAME:-lightspeed-redis-conn}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Pub/Sub Invoker Service Account (must match setup.sh)
PUBSUB_INVOKER_NAME="${PUBSUB_INVOKER_NAME:-pubsub-invoker}"
PUBSUB_INVOKER_SA="${PUBSUB_INVOKER_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Marketplace configuration
ENABLE_MARKETPLACE="${ENABLE_MARKETPLACE:-true}"

# Load balancer configuration
ENABLE_LOAD_BALANCER="${ENABLE_LOAD_BALANCER:-false}"
DOMAIN_NAME="${DOMAIN_NAME:-}"
LB_NAME="${LB_NAME:-lightspeed-lb}"
SERVICE_CONTROL_SERVICE_NAME="${SERVICE_CONTROL_SERVICE_NAME:-}"
PUBSUB_TOPIC="${PUBSUB_TOPIC:-marketplace-entitlements}"

# When PUBSUB_TOPIC is a fully-qualified path (projects/.../topics/...),
# the default derivation "${PUBSUB_TOPIC}-sub" produces an invalid
# subscription name.  Require PUBSUB_SUBSCRIPTION to be set explicitly.
if [[ "$PUBSUB_TOPIC" == projects/* && -z "${PUBSUB_SUBSCRIPTION:-}" ]]; then
    log_error "PUBSUB_TOPIC is a fully-qualified path but PUBSUB_SUBSCRIPTION is not set."
    log_error "Set PUBSUB_SUBSCRIPTION to a valid subscription name, e.g.:"
    log_error "  export PUBSUB_SUBSCRIPTION=\"marketplace-events-sub\""
    exit 1
fi
PUBSUB_SUBSCRIPTION="${PUBSUB_SUBSCRIPTION:-${PUBSUB_TOPIC}-sub}"

# Default images
AGENT_IMAGE="${AGENT_IMAGE:-}"
HANDLER_IMAGE="${HANDLER_IMAGE:-}"
# MCP image must be in GCR since Cloud Run doesn't support Quay.io directly
# See README.md for instructions to copy the image from Quay.io to GCR
MCP_IMAGE="${MCP_IMAGE:-gcr.io/${PROJECT_ID}/red-hat-lightspeed-mcp:latest}"

# Parse arguments
DEPLOY_SERVICE="all"  # all, handler, agent
ALLOW_UNAUTH=false
BUILD_IMAGE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --service)
            DEPLOY_SERVICE="$2"
            shift 2
            ;;
        --image)
            AGENT_IMAGE="$2"
            shift 2
            ;;
        --handler-image)
            HANDLER_IMAGE="$2"
            shift 2
            ;;
        --mcp-image)
            MCP_IMAGE="$2"
            shift 2
            ;;
        --allow-unauthenticated)
            ALLOW_UNAUTH=true
            shift
            ;;
        --build)
            BUILD_IMAGE=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Usage: $0 [--service all|handler|agent] [--image IMAGE] [--handler-image IMAGE] [--mcp-image IMAGE] [--allow-unauthenticated] [--build]"
            exit 1
            ;;
    esac
done

# Validate required variables
if [[ -z "$PROJECT_ID" ]]; then
    log_error "GOOGLE_CLOUD_PROJECT environment variable is required"
    exit 1
fi

if [[ "$ENABLE_LOAD_BALANCER" == "true" && -z "$DOMAIN_NAME" ]]; then
    log_error "DOMAIN_NAME is required when ENABLE_LOAD_BALANCER=true"
    echo "  export DOMAIN_NAME=your-domain.example.com"
    exit 1
fi

# Set default images if not specified
if [[ -z "$AGENT_IMAGE" ]]; then
    AGENT_IMAGE="gcr.io/${PROJECT_ID}/lightspeed-agent:${IMAGE_TAG}"
fi
if [[ -z "$HANDLER_IMAGE" ]]; then
    HANDLER_IMAGE="gcr.io/${PROJECT_ID}/${HANDLER_SERVICE_NAME}:${IMAGE_TAG}"
fi

log_info "Deploying to Cloud Run"
log_info "  Project: $PROJECT_ID"
log_info "  Region: $REGION"
log_info "  Service(s): $DEPLOY_SERVICE"
log_info "  Agent Image: $AGENT_IMAGE"
log_info "  Handler Image: $HANDLER_IMAGE"
log_info "  MCP Image: $MCP_IMAGE"

# =============================================================================
# Build images if requested
# =============================================================================
build_agent_image() {
    log_info "Building agent image..."

    gcloud builds submit \
        --tag "$AGENT_IMAGE" \
        --project "$PROJECT_ID" \
        --dockerfile Containerfile \
        .

    log_info "Image built: $AGENT_IMAGE"
}

build_handler_image() {
    log_info "Building marketplace handler image..."

    gcloud builds submit \
        --tag "$HANDLER_IMAGE" \
        --project "$PROJECT_ID" \
        --dockerfile Containerfile.marketplace-handler \
        .

    log_info "Image built: $HANDLER_IMAGE"
}

# =============================================================================
# Deploy using service YAML configs
# =============================================================================
deploy_agent() {
    log_info "Deploying agent with service.yaml..."

    # Create temporary file with substituted values
    local tmp_yaml
    tmp_yaml=$(mktemp)

    # Substitute variables in service.yaml
    # Note: Image substitution must happen BEFORE PROJECT_ID substitution
    sed -e "s|gcr.io/\${PROJECT_ID}/lightspeed-agent:latest|${AGENT_IMAGE}|g" \
        -e "s|\${MCP_IMAGE}|${MCP_IMAGE}|g" \
        -e "s|\${PROJECT_ID}|${PROJECT_ID}|g" \
        -e "s|\${REGION}|${REGION}|g" \
        -e "s|\${VERTEXAI_LOCATION}|${VERTEXAI_LOCATION}|g" \
        -e "s|\${SERVICE_NAME}|${SERVICE_NAME}|g" \
        -e "s|\${SERVICE_ACCOUNT_NAME}|${SERVICE_ACCOUNT_NAME}|g" \
        -e "s|\${DB_INSTANCE_NAME}|${DB_INSTANCE_NAME}|g" \
        -e "s|\${VPC_CONNECTOR_NAME}|${VPC_CONNECTOR_NAME}|g" \
        deploy/cloudrun/service.yaml > "$tmp_yaml"

    # Deploy using the YAML
    gcloud run services replace "$tmp_yaml" \
        --region "$REGION" \
        --project "$PROJECT_ID"

    # Set IAM policy if allowing unauthenticated
    if [[ "$ALLOW_UNAUTH" == "true" ]]; then
        log_info "Allowing unauthenticated access for agent..."
        gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
            --region "$REGION" \
            --project "$PROJECT_ID" \
            --member="allUsers" \
            --role="roles/run.invoker"
    fi

    # Cleanup
    rm -f "$tmp_yaml"
}

deploy_handler() {
    log_info "Deploying marketplace handler with marketplace-handler.yaml..."

    # Create temporary file with substituted values
    local tmp_yaml
    tmp_yaml=$(mktemp)

    # Substitute variables in marketplace-handler.yaml
    # Note: Image substitution must happen BEFORE PROJECT_ID substitution
    sed -e "s|gcr.io/\${PROJECT_ID}/marketplace-handler:latest|${HANDLER_IMAGE}|g" \
        -e "s|\${PROJECT_ID}|${PROJECT_ID}|g" \
        -e "s|\${REGION}|${REGION}|g" \
        -e "s|\${SERVICE_NAME}|${SERVICE_NAME}|g" \
        -e "s|\${SERVICE_ACCOUNT_NAME}|${SERVICE_ACCOUNT_NAME}|g" \
        -e "s|\${HANDLER_SERVICE_NAME}|${HANDLER_SERVICE_NAME}|g" \
        -e "s|\${DB_INSTANCE_NAME}|${DB_INSTANCE_NAME}|g" \
        -e "s|\${SERVICE_CONTROL_SERVICE_NAME}|${SERVICE_CONTROL_SERVICE_NAME}|g" \
        -e "s|\${VPC_CONNECTOR_NAME}|${VPC_CONNECTOR_NAME}|g" \
        deploy/cloudrun/marketplace-handler.yaml > "$tmp_yaml"

    # Deploy using the YAML
    gcloud run services replace "$tmp_yaml" \
        --region "$REGION" \
        --project "$PROJECT_ID"

    # Marketplace handler needs to be publicly accessible for Pub/Sub push
    if [[ "$ALLOW_UNAUTH" == "true" ]]; then
        log_info "Allowing unauthenticated access for handler..."
        gcloud run services add-iam-policy-binding "$HANDLER_SERVICE_NAME" \
            --region "$REGION" \
            --project "$PROJECT_ID" \
            --member="allUsers" \
            --role="roles/run.invoker"
    fi

    # Cleanup
    rm -f "$tmp_yaml"
}

# =============================================================================
# Configure Pub/Sub push subscription
# =============================================================================
configure_pubsub_push() {
    if [[ "$ENABLE_MARKETPLACE" != "true" ]]; then
        log_info "Skipping Pub/Sub push configuration (ENABLE_MARKETPLACE=$ENABLE_MARKETPLACE)"
        return
    fi

    log_info "Configuring Pub/Sub push subscription..."

    # Get the marketplace-handler URL for the push endpoint
    local handler_url
    handler_url=$(gcloud run services describe "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --format='value(status.url)' 2>/dev/null || echo "")

    if [[ -z "$handler_url" ]]; then
        log_warn "Could not retrieve $HANDLER_SERVICE_NAME URL. Skipping Pub/Sub push configuration."
        log_warn "Run deploy.sh again after the handler is deployed."
        return
    fi

    local push_endpoint="${handler_url}/dcr"

    # Grant the Pub/Sub Invoker SA permission to invoke the marketplace-handler.
    # This is a service-level binding (not project-level), following least privilege.
    log_info "Granting roles/run.invoker to Pub/Sub Invoker SA on $HANDLER_SERVICE_NAME..."
    gcloud run services add-iam-policy-binding "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --member="serviceAccount:$PUBSUB_INVOKER_SA" \
        --role="roles/run.invoker" \
        --quiet || true

    # For cross-project topics (fully-qualified path), the Pub/Sub Invoker SA
    # is the account linked in the Google Cloud Marketplace Producer Portal and
    # has permission to subscribe to the external topic.  We must impersonate it
    # because the caller's personal account does not have that permission.
    local impersonate_flag=""
    if [[ "$PUBSUB_TOPIC" == projects/* ]]; then
        impersonate_flag="--impersonate-service-account=$PUBSUB_INVOKER_SA"
        log_info "Using service account impersonation for cross-project topic"
    fi

    # Create or update the push subscription
    if gcloud pubsub subscriptions describe "$PUBSUB_SUBSCRIPTION" --project="$PROJECT_ID" &>/dev/null; then
        log_info "Updating existing subscription '$PUBSUB_SUBSCRIPTION' with push endpoint..."
        gcloud pubsub subscriptions update "$PUBSUB_SUBSCRIPTION" \
            --push-endpoint="$push_endpoint" \
            --push-auth-service-account="$PUBSUB_INVOKER_SA" \
            --ack-deadline=60 \
            --project="$PROJECT_ID" \
            --quiet
    else
        log_info "Creating push subscription '$PUBSUB_SUBSCRIPTION'..."
        # shellcheck disable=SC2086
        gcloud pubsub subscriptions create "$PUBSUB_SUBSCRIPTION" \
            --topic="$PUBSUB_TOPIC" \
            --push-endpoint="$push_endpoint" \
            --push-auth-service-account="$PUBSUB_INVOKER_SA" \
            --ack-deadline=60 \
            --project="$PROJECT_ID" \
            $impersonate_flag
    fi

    log_info "Pub/Sub push subscription configured:"
    log_info "  Subscription: $PUBSUB_SUBSCRIPTION"
    log_info "  Push endpoint: $push_endpoint"
    log_info "  Auth SA: $PUBSUB_INVOKER_SA"
}

# =============================================================================
# Configure Google Cloud Load Balancer
# =============================================================================
setup_load_balancer() {
    if [[ "$ENABLE_LOAD_BALANCER" != "true" ]]; then
        return
    fi

    log_info "Setting up Google Cloud Load Balancer..."

    # -------------------------------------------------------------------------
    # Create serverless NEGs for each Cloud Run service
    # -------------------------------------------------------------------------
    log_info "Creating serverless NEGs..."

    if ! gcloud compute network-endpoint-groups describe "${LB_NAME}-agent-neg" \
        --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute network-endpoint-groups create "${LB_NAME}-agent-neg" \
            --region="$REGION" \
            --network-endpoint-type=serverless \
            --cloud-run-service="$SERVICE_NAME" \
            --project="$PROJECT_ID"
        log_info "NEG '${LB_NAME}-agent-neg' created"
    else
        log_info "NEG '${LB_NAME}-agent-neg' already exists"
    fi

    if ! gcloud compute network-endpoint-groups describe "${LB_NAME}-handler-neg" \
        --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute network-endpoint-groups create "${LB_NAME}-handler-neg" \
            --region="$REGION" \
            --network-endpoint-type=serverless \
            --cloud-run-service="$HANDLER_SERVICE_NAME" \
            --project="$PROJECT_ID"
        log_info "NEG '${LB_NAME}-handler-neg' created"
    else
        log_info "NEG '${LB_NAME}-handler-neg' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create backend services
    # -------------------------------------------------------------------------
    log_info "Creating backend services..."

    if ! gcloud compute backend-services describe "${LB_NAME}-agent-backend" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute backend-services create "${LB_NAME}-agent-backend" \
            --global \
            --project="$PROJECT_ID"
        gcloud compute backend-services add-backend "${LB_NAME}-agent-backend" \
            --global \
            --network-endpoint-group="${LB_NAME}-agent-neg" \
            --network-endpoint-group-region="$REGION" \
            --project="$PROJECT_ID"
        log_info "Backend service '${LB_NAME}-agent-backend' created"
    else
        log_info "Backend service '${LB_NAME}-agent-backend' already exists"
    fi

    if ! gcloud compute backend-services describe "${LB_NAME}-handler-backend" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute backend-services create "${LB_NAME}-handler-backend" \
            --global \
            --project="$PROJECT_ID"
        gcloud compute backend-services add-backend "${LB_NAME}-handler-backend" \
            --global \
            --network-endpoint-group="${LB_NAME}-handler-neg" \
            --network-endpoint-group-region="$REGION" \
            --project="$PROJECT_ID"
        log_info "Backend service '${LB_NAME}-handler-backend' created"
    else
        log_info "Backend service '${LB_NAME}-handler-backend' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create URL map with path-based routing
    # -------------------------------------------------------------------------
    log_info "Creating URL map..."

    if ! gcloud compute url-maps describe "${LB_NAME}-url-map" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        # Default backend → agent service
        gcloud compute url-maps create "${LB_NAME}-url-map" \
            --default-service="${LB_NAME}-agent-backend" \
            --global \
            --project="$PROJECT_ID"
        # Path rule: /dcr → marketplace handler, everything else → agent
        gcloud compute url-maps add-path-matcher "${LB_NAME}-url-map" \
            --path-matcher-name=marketplace \
            --default-service="${LB_NAME}-agent-backend" \
            --path-rules="/dcr=${LB_NAME}-handler-backend" \
            --new-hosts="*" \
            --global \
            --project="$PROJECT_ID"
        log_info "URL map '${LB_NAME}-url-map' created"
    else
        log_info "URL map '${LB_NAME}-url-map' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create HTTPS proxy with managed SSL certificate
    # -------------------------------------------------------------------------
    log_info "Creating HTTPS proxy..."

    if ! gcloud compute target-https-proxies describe "${LB_NAME}-https-proxy" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute target-https-proxies create "${LB_NAME}-https-proxy" \
            --ssl-certificates="${LB_NAME}-cert" \
            --url-map="${LB_NAME}-url-map" \
            --global \
            --project="$PROJECT_ID"
        log_info "HTTPS proxy '${LB_NAME}-https-proxy' created"
    else
        log_info "HTTPS proxy '${LB_NAME}-https-proxy' already exists"
    fi

    # -------------------------------------------------------------------------
    # Create global forwarding rule
    # -------------------------------------------------------------------------
    log_info "Creating forwarding rule..."

    if ! gcloud compute forwarding-rules describe "${LB_NAME}-forwarding-rule" \
        --global --project="$PROJECT_ID" &>/dev/null; then
        gcloud compute forwarding-rules create "${LB_NAME}-forwarding-rule" \
            --global \
            --target-https-proxy="${LB_NAME}-https-proxy" \
            --address="${LB_NAME}-ip" \
            --ports=443 \
            --project="$PROJECT_ID"
        log_info "Forwarding rule '${LB_NAME}-forwarding-rule' created"
    else
        log_info "Forwarding rule '${LB_NAME}-forwarding-rule' already exists"
    fi

    # -------------------------------------------------------------------------
    # Override Cloud Run ingress to internal-and-cloud-load-balancing
    # -------------------------------------------------------------------------
    log_info "Updating Cloud Run ingress to internal-and-cloud-load-balancing..."

    gcloud run services update "$SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --ingress=internal-and-cloud-load-balancing \
        --quiet

    gcloud run services update "$HANDLER_SERVICE_NAME" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --ingress=internal-and-cloud-load-balancing \
        --quiet

    log_info "Cloud Run ingress updated for both services"
}

update_service_ingress() {
    if [[ "$ENABLE_LOAD_BALANCER" != "true" ]]; then
        return
    fi

    local service_name="$1"
    log_info "Updating ingress for $service_name to internal-and-cloud-load-balancing..."
    gcloud run services update "$service_name" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --ingress=internal-and-cloud-load-balancing \
        --quiet
}

# =============================================================================
# Main deployment
# =============================================================================

# Build images if requested
if [[ "$BUILD_IMAGE" == "true" ]]; then
    case "$DEPLOY_SERVICE" in
        all)
            build_handler_image
            build_agent_image
            ;;
        handler)
            build_handler_image
            ;;
        agent)
            build_agent_image
            ;;
    esac
fi

# Deploy based on service selection
case "$DEPLOY_SERVICE" in
    all)
        deploy_handler
        configure_pubsub_push
        deploy_agent
        setup_load_balancer
        ;;
    handler)
        deploy_handler
        configure_pubsub_push
        update_service_ingress "$HANDLER_SERVICE_NAME"
        ;;
    agent)
        deploy_agent
        update_service_ingress "$SERVICE_NAME"
        ;;
    *)
        log_error "Unknown service: $DEPLOY_SERVICE"
        echo "Valid services: all, handler, agent"
        exit 1
        ;;
esac

# =============================================================================
# Post-deployment
# =============================================================================

log_info "Deployment complete!"
echo ""

# Get and display service URLs based on what was deployed
show_service_info() {
    local service_name="$1"
    local service_url

    service_url=$(gcloud run services describe "$service_name" \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --format='value(status.url)' 2>/dev/null || echo "")

    if [[ -n "$service_url" ]]; then
        log_info "$service_name URL: $service_url"
        echo "  Test: curl $service_url/health"
    else
        log_warn "Could not retrieve $service_name URL"
    fi
}

# Show info for deployed services
case "$DEPLOY_SERVICE" in
    all)
        echo ""
        show_service_info "$HANDLER_SERVICE_NAME"
        echo ""
        show_service_info "$SERVICE_NAME"

        # Update AGENT_PROVIDER_URL (agent base URL) and MARKETPLACE_HANDLER_URL
        # on the agent service so the AgentCard advertises the correct URLs.
        # Note: AGENT_PROVIDER_ORGANIZATION_URL (JWT audience for DCR) is set
        # in service.yaml and does NOT change per deployment — it's the
        # provider's website (e.g., https://www.redhat.com).
        service_url=$(gcloud run services describe "$SERVICE_NAME" \
            --region="$REGION" \
            --project="$PROJECT_ID" \
            --format='value(status.url)' 2>/dev/null)
        handler_url=$(gcloud run services describe "$HANDLER_SERVICE_NAME" \
            --region="$REGION" \
            --project="$PROJECT_ID" \
            --format='value(status.url)' 2>/dev/null || echo "")

        if [[ -n "$service_url" ]]; then
            env_vars="AGENT_PROVIDER_URL=$service_url"
            if [[ -n "$handler_url" ]]; then
                env_vars="$env_vars,MARKETPLACE_HANDLER_URL=$handler_url"
            else
                log_warn "Could not retrieve $HANDLER_SERVICE_NAME URL. MARKETPLACE_HANDLER_URL not set."
                log_warn "DCR endpoints in the AgentCard will fall back to AGENT_PROVIDER_URL."
            fi
            log_info "Updating agent env vars with service URLs"
            gcloud run services update "$SERVICE_NAME" \
                --region="$REGION" \
                --project="$PROJECT_ID" \
                --update-env-vars="$env_vars" \
                --quiet 2>&1 | grep -v "Deploying\|Creating\|Routing" || true
            log_info "Agent env vars updated successfully"
        fi

        echo ""
        echo "Architecture:"
        echo "  1. Marketplace Handler receives Pub/Sub events and DCR requests"
        echo "  2. Agent handles A2A protocol and user interactions"
        echo ""
        echo "Test endpoints:"
        echo "  Handler health: curl \$(gcloud run services describe $HANDLER_SERVICE_NAME --region=$REGION --format='value(status.url)')/health"
        echo "  Agent card:     curl \$(gcloud run services describe $SERVICE_NAME --region=$REGION --format='value(status.url)')/.well-known/agent.json"
        ;;
    handler)
        echo ""
        show_service_info "$HANDLER_SERVICE_NAME"
        echo ""
        echo "The marketplace handler is ready to receive:"
        echo "  - Pub/Sub events from Google Cloud Marketplace"
        echo "  - DCR requests from Gemini Enterprise"
        ;;
    agent)
        echo ""
        show_service_info "$SERVICE_NAME"

        # Update AGENT_PROVIDER_URL (agent base URL) and MARKETPLACE_HANDLER_URL
        # on the agent service. AGENT_PROVIDER_ORGANIZATION_URL is set in
        # service.yaml and does NOT change per deployment.
        service_url=$(gcloud run services describe "$SERVICE_NAME" \
            --region="$REGION" \
            --project="$PROJECT_ID" \
            --format='value(status.url)' 2>/dev/null)
        handler_url=$(gcloud run services describe "$HANDLER_SERVICE_NAME" \
            --region="$REGION" \
            --project="$PROJECT_ID" \
            --format='value(status.url)' 2>/dev/null || echo "")

        if [[ -n "$service_url" ]]; then
            env_vars="AGENT_PROVIDER_URL=$service_url"
            if [[ -n "$handler_url" ]]; then
                env_vars="$env_vars,MARKETPLACE_HANDLER_URL=$handler_url"
            else
                log_warn "Could not retrieve $HANDLER_SERVICE_NAME URL. MARKETPLACE_HANDLER_URL not set."
                log_warn "DCR endpoints in the AgentCard will fall back to AGENT_PROVIDER_URL."
            fi
            log_info "Updating agent env vars with service URLs"
            gcloud run services update "$SERVICE_NAME" \
                --region="$REGION" \
                --project="$PROJECT_ID" \
                --update-env-vars="$env_vars" \
                --quiet 2>&1 | grep -v "Deploying\|Creating\|Routing" || true
            log_info "Agent env vars updated successfully"
        fi

        echo ""
        echo "Test the agent:"
        echo "  curl \$(gcloud run services describe $SERVICE_NAME --region=$REGION --format='value(status.url)')/.well-known/agent-card.json"
        ;;
esac

# Show load balancer info if enabled
if [[ "$ENABLE_LOAD_BALANCER" == "true" ]]; then
    echo ""
    log_info "=========================================="
    log_info "Load Balancer"
    log_info "=========================================="

    LB_IP=$(gcloud compute addresses describe "${LB_NAME}-ip" \
        --global --project="$PROJECT_ID" \
        --format='value(address)' 2>/dev/null || echo "unknown")

    CERT_STATUS=$(gcloud compute ssl-certificates describe "${LB_NAME}-cert" \
        --global --project="$PROJECT_ID" \
        --format='value(managed.status)' 2>/dev/null || echo "unknown")

    echo ""
    echo "  URL:         https://$DOMAIN_NAME"
    echo "  Static IP:   $LB_IP"
    echo "  SSL status:  $CERT_STATUS"
    echo ""
    echo "  Routing:"
    echo "    /    → $SERVICE_NAME (agent)"
    echo "    /dcr → $HANDLER_SERVICE_NAME (marketplace handler)"
    echo ""
    if [[ "$CERT_STATUS" != "ACTIVE" ]]; then
        log_warn "SSL certificate is not yet active (status: $CERT_STATUS)"
        log_warn "Ensure DNS A record points $DOMAIN_NAME → $LB_IP"
        log_warn "Provisioning can take up to 60 minutes after DNS propagation."
    fi
fi

echo ""
echo "View logs:"
echo "  gcloud run services logs read $HANDLER_SERVICE_NAME --region=$REGION --project=$PROJECT_ID"
echo "  gcloud run services logs read $SERVICE_NAME --region=$REGION --project=$PROJECT_ID"
