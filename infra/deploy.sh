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
# Owned by this script, NOT by the CloudFormation stack. The service can't start
# until the image is in the registry, so the repo has to exist before the stack
# is deployed — and a stack that also declares it is rejected with
# AWS::EarlyValidation::ResourceExistenceCheck. teardown.sh removes it.
if ! aws ecr describe-repositories --region "$REGION" --repository-names "$PROJECT" >/dev/null 2>&1; then
  aws ecr create-repository --region "$REGION" --repository-name "$PROJECT" \
    --image-scanning-configuration scanOnPush=true >/dev/null
  # SHA-tagged images accumulate on every deploy; keep the last 10.
  aws ecr put-lifecycle-policy --region "$REGION" --repository-name "$PROJECT" \
    --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"keep last 10","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":10},"action":{"type":"expire"}}]}' >/dev/null
fi
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
  # openssl, not `tr </dev/urandom | head -c`: head closes the pipe at 24 bytes,
  # tr takes SIGPIPE, and `set -o pipefail` turns that into exit 141 — the script
  # kills itself immediately after writing a perfectly good password file.
  openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | cut -c1-24 > "$PW_FILE"
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

# RDS is not publicly accessible — by design (§7: "keep the instance in a
# private subnet, reachable only from your ECS task's security group"). There is
# therefore no route to it from this laptop, so the index MUST be built from
# inside the running task via ECS Exec.
say "Building the pgvector index inside the running task"
CLUSTER_NAME="$(out ClusterName)"; SERVICE_NAME="$(out ServiceName)"
TASK_ARN="$(aws ecs list-tasks --region "$REGION" --cluster "$CLUSTER_NAME" \
  --service-name "$SERVICE_NAME" --desired-status RUNNING --query 'taskArns[0]' --output text)"

seed_ok=0
if [ "$TASK_ARN" != "None" ] && [ -n "$TASK_ARN" ]; then
  # --non-interactive isn't supported; --interactive with a one-shot command
  # returns when the command exits.
  if aws ecs execute-command --region "$REGION" --cluster "$CLUSTER_NAME" \
       --task "$TASK_ARN" --container agent --interactive \
       --command "python -m rag.build_index"; then
    seed_ok=1
  fi
fi

if [ "$seed_ok" -eq 0 ]; then
  cat <<'WARN'

  WARNING: could not seed the pgvector index automatically.

  ECS Exec requires the session-manager-plugin on this machine:
      brew install --cask session-manager-plugin

  Then seed it by hand (the ALB /ready endpoint will report not_ready until you do):
      bash infra/seed_index.sh

WARN
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
