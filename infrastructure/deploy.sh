#!/usr/bin/env bash
#
# ClauseGuard — deploy.sh
#
# Builds the web and worker Docker images, pushes them to their ECR
# repositories, and then deploys/updates the ECS services.
#
# Prerequisites:
#   - infrastructure/provision.py has already been run successfully
#     (infrastructure/deployment_state.json must exist).
#   - Docker is installed and running locally.
#   - AWS CLI v2 is installed and configured with credentials that have
#     permission to push to ECR and update ECS services.
#
# Usage:
#   ./infrastructure/deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_FILE="$SCRIPT_DIR/deployment_state.json"
REGION="ap-south-1"

if [ ! -f "$STATE_FILE" ]; then
  echo "ERROR: $STATE_FILE not found. Run 'python3 infrastructure/provision.py' first." >&2
  exit 1
fi

ECR_WEB_URI=$(python3 -c "import json;print(json.load(open('$STATE_FILE'))['ecr_web_uri'])")
ECR_WORKER_URI=$(python3 -c "import json;print(json.load(open('$STATE_FILE'))['ecr_worker_uri'])")
ACCOUNT_ID=$(python3 -c "import json;print(json.load(open('$STATE_FILE'))['account_id'])")

echo "==> Logging in to ECR ($REGION)"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> Building web image"
docker build -t "${ECR_WEB_URI}:latest" -f "$PROJECT_ROOT/web/Dockerfile" "$PROJECT_ROOT/web"

echo "==> Building worker image"
docker build -t "${ECR_WORKER_URI}:latest" -f "$PROJECT_ROOT/worker/Dockerfile" "$PROJECT_ROOT/worker"

echo "==> Pushing web image"
docker push "${ECR_WEB_URI}:latest"

echo "==> Pushing worker image"
docker push "${ECR_WORKER_URI}:latest"

echo "==> Registering new task definition revisions and deploying ECS services"
python3 "$SCRIPT_DIR/deploy_services.py"

echo "==> Deployment complete."
