#!/usr/bin/env bash
# Build the pgvector index inside the running ECS task.
#
#   bash infra/seed_index.sh
#
# Separate from deploy.sh because it's the one step that can legitimately need a
# retry: the task has to be RUNNING and ECS Exec's SSM channel has to be up,
# which lags the service reaching a stable state by a few seconds.
#
# It runs inside the task rather than from here because RDS is not publicly
# accessible — its security group admits only the ECS task's security group, so
# no route to the database exists from outside the VPC. That's the intended
# design (§7), not an obstacle to work around by opening the database up.
set -euo pipefail

PROJECT="${PROJECT_NAME:-ecom-agent}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-south-1)}"
CLUSTER="${PROJECT}-cluster"
SERVICE="${PROJECT}-service"

command -v session-manager-plugin >/dev/null || {
  echo "session-manager-plugin is required for ECS Exec:"
  echo "    brew install --cask session-manager-plugin"
  exit 1
}

TASK="$(aws ecs list-tasks --region "$REGION" --cluster "$CLUSTER" \
  --service-name "$SERVICE" --desired-status RUNNING \
  --query 'taskArns[0]' --output text)"
[ "$TASK" != "None" ] && [ -n "$TASK" ] || { echo "No RUNNING task in $SERVICE."; exit 1; }

URL="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "${PROJECT}-stack" \
  --query "Stacks[0].Outputs[?OutputKey=='AlbUrl'].OutputValue" --output text)"

echo "==> Seeding the index in task ${TASK##*/}"
# Run detached with nohup and poll, rather than waiting on the interactive
# session. The embedding call takes ~10s and the ECS Exec channel closes with
# "Cannot perform start session: EOF" before it finishes — the work is killed
# with it. Backgrounding inside the container survives the session teardown.
aws ecs execute-command --region "$REGION" --cluster "$CLUSTER" \
  --task "$TASK" --container agent --interactive \
  --command "sh -c 'nohup python -m rag.build_index > /tmp/seed.log 2>&1 & echo started'" >/dev/null 2>&1 || true

echo "==> Waiting for the index to appear (polling /ready)"
for i in $(seq 20); do
  sleep 10
  if curl -s --max-time 20 "$URL/ready" | grep -q '"status": *"ready"'; then
    echo "index ready after $(( i * 10 ))s"
    curl -s "$URL/ready"; echo
    exit 0
  fi
  printf "."
done

echo
echo "Index still not ready. Last response:"
curl -s "$URL/ready"; echo
echo "Container-side log:"
aws ecs execute-command --region "$REGION" --cluster "$CLUSTER" \
  --task "$TASK" --container agent --interactive --command "cat /tmp/seed.log" 2>&1 | tail -20
exit 1
