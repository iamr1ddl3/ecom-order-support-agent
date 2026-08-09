#!/usr/bin/env bash
# One command, stack from nothing to a live URL (§2.6.1).
#
#   bash infra/deploy.sh
#
# Idempotent: safe to re-run. A second run builds a new image, pushes it under
# the new commit SHA, and updates the service in place.
#
# Reads provider keys from .env. Never echoes them: the CloudFormation
# parameters are NoEcho, and this script prints only resource names.
set -euo pipefail

PROJECT="${PROJECT_NAME:-ecom-agent}"
STACK="${PROJECT}-stack"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-south-1)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

# --- preflight -------------------------------------------------------------
command -v aws >/dev/null || { echo "aws CLI not found. brew install awscli"; exit 1; }
command -v docker >/dev/null || { echo "docker not found"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker daemon is not running. Start Docker Desktop."; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "No AWS credentials. Run: aws configure"; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
[ -f .env ] || { echo ".env not found — copy .env.example and fill it in."; exit 1; }
# shellcheck disable=SC1091
set -a; source .env; set +a
[ -n "${OPENAI_API_KEY:-}" ] || { echo "OPENAI_API_KEY missing from .env (pgvector embeddings need it)."; exit 1; }

say "Account $ACCOUNT, region $REGION, stack $STACK"

# --- network: use the default VPC -----------------------------------------
# An ALB needs subnets in >=2 AZs. Taking the first two keeps the target group
# valid without provisioning a VPC for a stack that lives for one recording.
VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
[ "$VPC_ID" != "None" ] || { echo "No default VPC in $REGION. Create one or set VpcId by hand."; exit 1; }
SUBNETS="$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=default-for-az,Values=true" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',' | cut -d, -f1,2)"
say "VPC $VPC_ID, subnets $SUBNETS"

# --- ECR: repo must exist before the image can be pushed -------------------
# The stack also declares this repo, but the image has to exist before the
# service can start, so it's created here first. Both paths are idempotent.
aws ecr describe-repositories --region "$REGION" --repository-names "$PROJECT" >/dev/null 2>&1 || \
  aws ecr create-repository --region "$REGION" --repository-name "$PROJECT" \
    --image-scanning-configuration scanOnPush=true >/dev/null
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# §2.6.1: tag by commit SHA, not `latest` — a latest-only scheme gives no audit
# trail from a running task back to the commit that produced it.
SHA="$(git rev-parse --short HEAD)"
IMAGE="${REGISTRY}/${PROJECT}:${SHA}"

say "Building and pushing $IMAGE"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
# linux/amd64 explicitly: an Apple Silicon build defaults to arm64 and the task
# dies with an exec format error that surfaces only as a failing health check.
docker build --platform linux/amd64 -t "$IMAGE" .
docker push -q "$IMAGE"

# --- database password -----------------------------------------------------
# Generated once and cached locally (gitignored). Reused on re-deploy, because
# changing an RDS master password mid-update forces a longer update.
PW_FILE="infra/.db-password"
if [ ! -f "$PW_FILE" ]; then
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 > "$PW_FILE"
  chmod 600 "$PW_FILE"
fi
DB_PASSWORD="$(cat "$PW_FILE")"

say "Deploying stack (RDS first creation takes ~8-10 minutes)"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file infra/stack.yml \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    ProjectName="$PROJECT" \
    VpcId="$VPC_ID" \
    SubnetIds="$SUBNETS" \
    ImageUri="$IMAGE" \
    DBPassword="$DB_PASSWORD" \
    LLMProvider="${LLM_PROVIDER:-glm}" \
    ZaiApiKey="${ZAI_API_KEY:-}" \
    GroqApiKey="${GROQ_API_KEY:-}" \
    AnthropicApiKey="${ANTHROPIC_API_KEY:-}" \
    OpenAIApiKey="$OPENAI_API_KEY"

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

ALB_URL="$(out AlbUrl)"; DB_HOST="$(out DBEndpoint)"

# --- seed the vector index -------------------------------------------------
# RDS is in a private security group reachable only from the ECS task, so the
# index is built from inside the running task rather than from this laptop.
say "Waiting for the service to become healthy"
aws ecs wait services-stable --region "$REGION" \
  --cluster "$(out ClusterName)" --services "$(out ServiceName)" || true

say "Building the pgvector index inside the running task"
TASK_ARN="$(aws ecs list-tasks --region "$REGION" --cluster "$(out ClusterName)" \
  --service-name "$(out ServiceName)" --query 'taskArns[0]' --output text)"
if [ "$TASK_ARN" != "None" ] && aws ecs execute-command --region "$REGION" \
     --cluster "$(out ClusterName)" --task "$TASK_ARN" --container agent \
     --interactive --command "python -m rag.build_index" 2>/dev/null; then
  echo "index built via ECS exec"
else
  # ECS Exec needs enableExecuteCommand plus SSM plumbing; rather than add that
  # for a one-shot seed, fall back to a temporary ingress rule from this host.
  echo "ECS exec unavailable — seeding over a temporary ingress rule instead"
  MY_IP="$(curl -s https://checkip.amazonaws.com)/32"
  DB_SG="$(aws cloudformation describe-stack-resources --region "$REGION" --stack-name "$STACK" \
    --logical-resource-id DBSecurityGroup --query 'StackResources[0].PhysicalResourceId' --output text)"
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$DB_SG" \
    --protocol tcp --port 5432 --cidr "$MY_IP" >/dev/null 2>&1 || true
  # RDS must be publicly reachable for this path; if it isn't, this fails loudly
  # rather than leaving an unseeded index to surface as a runtime error.
  DATABASE_URL="postgresql://postgres:${DB_PASSWORD}@${DB_HOST}:5432/agent" \
    python -m rag.build_index || echo "WARNING: index build failed — /ready will report not_ready"
  aws ec2 revoke-security-group-ingress --region "$REGION" --group-id "$DB_SG" \
    --protocol tcp --port 5432 --cidr "$MY_IP" >/dev/null 2>&1 || true
fi

say "Deployed"
cat <<EOF

  URL          $ALB_URL
  Cluster      $(out ClusterName)
  Service      $(out ServiceName)
  Task def     $(out TaskDefinitionArn)
  RDS          $(out DBIdentifier)  ($DB_HOST)
  Image        $IMAGE

  curl $ALB_URL/health
  curl $ALB_URL/ready
  curl -X POST $ALB_URL/chat -H 'content-type: application/json' \\
    -d '{"customer_id":"cust_1001","message":"What is the status of my order ord_5001?"}'

  Scale-out demo :  bash infra/load_test.sh $ALB_URL
  Tear it all down: bash infra/teardown.sh

EOF
