#!/usr/bin/env bash
# Remove every AWS resource this project created (§2.6.5).
#
#   bash infra/teardown.sh
#
# RDS bills continuously while it exists, so this has to actually work — §7 notes
# that a teardown script which silently fails on a dependency ordering issue is a
# common real mistake, not a hypothetical one. Two known ordering hazards are
# handled explicitly:
#
#   1. ECR won't delete while images exist. The repo carries DeletionPolicy:
#      Retain precisely so a non-empty repo can't fail the stack delete; it is
#      emptied and removed here, after the stack is gone.
#   2. The ECS service must scale to zero before the ALB target group can
#      deregister cleanly. CloudFormation usually sequences this itself, but it
#      can time out waiting on draining targets, so desired count is zeroed first.
#
# Ends by listing anything left behind, so "it said done" and "it is gone" are
# the same statement.
set -uo pipefail   # deliberately not -e: teardown continues past absent resources

PROJECT="${PROJECT_NAME:-ecom-agent}"
STACK="${PROJECT}-stack"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-south-1)}"

say() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

aws sts get-caller-identity >/dev/null 2>&1 || { echo "No AWS credentials."; exit 1; }
say "Tearing down $STACK in $REGION"

# --- 1. scale the service to zero -----------------------------------------
CLUSTER="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ClusterName'].OutputValue" --output text 2>/dev/null)"
SERVICE="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceName'].OutputValue" --output text 2>/dev/null)"

if [ -n "${CLUSTER:-}" ] && [ "$CLUSTER" != "None" ]; then
  say "Draining ECS service $SERVICE"
  aws ecs update-service --region "$REGION" --cluster "$CLUSTER" --service "$SERVICE" \
    --desired-count 0 >/dev/null 2>&1 && \
    aws ecs wait services-stable --region "$REGION" --cluster "$CLUSTER" --services "$SERVICE" 2>/dev/null
fi

# --- 2. delete the stack ---------------------------------------------------
say "Deleting the CloudFormation stack (RDS teardown takes a few minutes)"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK" 2>/dev/null
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK" 2>/dev/null \
  && echo "stack deleted" \
  || echo "stack delete finished with warnings — the sweep below reports what is left"

# --- 3. ECR: empty, then remove -------------------------------------------
# Retained by DeletionPolicy, so it outlives the stack by design.
if aws ecr describe-repositories --region "$REGION" --repository-names "$PROJECT" >/dev/null 2>&1; then
  say "Emptying and deleting the ECR repository"
  IMAGES="$(aws ecr list-images --region "$REGION" --repository-name "$PROJECT" \
    --query 'imageIds[*]' --output json 2>/dev/null)"
  if [ -n "$IMAGES" ] && [ "$IMAGES" != "[]" ]; then
    aws ecr batch-delete-image --region "$REGION" --repository-name "$PROJECT" \
      --image-ids "$IMAGES" >/dev/null 2>&1
  fi
  aws ecr delete-repository --region "$REGION" --repository-name "$PROJECT" --force >/dev/null 2>&1 \
    && echo "ECR repository deleted"
fi

# --- 4. log group ----------------------------------------------------------
aws logs delete-log-group --region "$REGION" --log-group-name "/ecs/${PROJECT}" >/dev/null 2>&1 \
  && echo "log group deleted"

rm -f infra/.db-password

# --- 5. verify, don't assume ----------------------------------------------
say "Sweep: anything still alive?"
LEFT=0
check() {  # label, command producing output when the resource still exists
  local found; found="$(eval "$2" 2>/dev/null)"
  if [ -n "$found" ] && [ "$found" != "None" ] && [ "$found" != "[]" ]; then
    echo "  STILL PRESENT  $1: $found"; LEFT=1
  else
    echo "  gone           $1"
  fi
}
check "CloudFormation stack" "aws cloudformation describe-stacks --region $REGION --stack-name $STACK --query 'Stacks[0].StackStatus' --output text"
check "ECS cluster"          "aws ecs describe-clusters --region $REGION --clusters ${PROJECT}-cluster --query 'clusters[?status==\`ACTIVE\`].clusterName' --output text"
check "RDS instance"         "aws rds describe-db-instances --region $REGION --db-instance-identifier ${PROJECT}-db --query 'DBInstances[0].DBInstanceIdentifier' --output text"
check "Load balancer"        "aws elbv2 describe-load-balancers --region $REGION --names ${PROJECT}-alb --query 'LoadBalancers[0].LoadBalancerName' --output text"
check "Target group"         "aws elbv2 describe-target-groups --region $REGION --names ${PROJECT}-tg --query 'TargetGroups[0].TargetGroupName' --output text"
check "ECR repository"       "aws ecr describe-repositories --region $REGION --repository-names $PROJECT --query 'repositories[0].repositoryName' --output text"

echo
if [ "$LEFT" -eq 0 ]; then
  echo "All resources removed. Nothing is billing."
else
  echo "Some resources remain (listed above). Re-run this script — RDS and ALB"
  echo "deletions are asynchronous and a second pass usually clears them."
  exit 1
fi
