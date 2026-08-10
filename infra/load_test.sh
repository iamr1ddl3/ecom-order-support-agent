#!/usr/bin/env bash
# Trigger a real scale-out and show the task count rising (§2.6.3).
#
#   bash infra/load_test.sh http://your-alb-dns
#
# §2.6.3 asks for evidence, not a description: "a simple loop firing several
# concurrent requests at your ALB URL is enough". No Apache Bench needed.
#
# It prints the ECS running-task count every 15s alongside the load, so a single
# screen recording shows the cause and the effect together — which is the shot
# the video needs.
set -uo pipefail

URL="${1:?usage: bash infra/load_test.sh http://alb-dns}"
PROJECT="${PROJECT_NAME:-ecom-agent}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-south-1)}"
CLUSTER="${PROJECT}-cluster"
SERVICE="${PROJECT}-service"
CONCURRENCY="${CONCURRENCY:-12}"
DURATION="${DURATION:-420}"   # 7 min: target-tracking needs ~3 CPU datapoints

echo "Load: ${CONCURRENCY} workers against ${URL} for ${DURATION}s"
echo "Autoscaling: CPU target 50%, min 1 / max 4 tasks"
echo

# /chat drives real CPU — an LLM round trip plus an MCP subprocess spawn per
# request. Hitting /health instead would prove nothing: it returns a dict and
# burns no CPU, so the metric the policy tracks would never move.
worker() {
  local end=$(( SECONDS + DURATION ))
  while [ $SECONDS -lt $end ]; do
    curl -s -o /dev/null --max-time 60 -X POST "${URL}/chat" \
      -H 'content-type: application/json' \
      -d '{"customer_id":"cust_1001","message":"What is the status of my order ord_5001?"}'
  done
}

for _ in $(seq "$CONCURRENCY"); do worker & done

printf "%-9s %-8s %-8s %s\n" "TIME" "RUNNING" "DESIRED" "CPU%"
START=$SECONDS
while [ $(( SECONDS - START )) -lt $DURATION ]; do
  read -r RUNNING DESIRED <<< "$(aws ecs describe-services --region "$REGION" \
    --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].[runningCount,desiredCount]' --output text 2>/dev/null)"
  CPU="$(aws cloudwatch get-metric-statistics --region "$REGION" \
    --namespace AWS/ECS --metric-name CPUUtilization \
    --dimensions Name=ClusterName,Value="$CLUSTER" Name=ServiceName,Value="$SERVICE" \
    --start-time "$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '5 min ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 60 --statistics Average \
    --query 'sort_by(Datapoints,&Timestamp)[-1].Average' --output text 2>/dev/null)"
  printf "%-9s %-8s %-8s %s\n" "$(( SECONDS - START ))s" "${RUNNING:-?}" "${DESIRED:-?}" "${CPU:-...}"
  sleep 15
done

wait
echo
echo "Load stopped. Scale-in follows the 300s cooldown, so the count stays"
echo "elevated for a few minutes — that is the policy working, not a leak."
