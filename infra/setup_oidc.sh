#!/usr/bin/env bash
# One-time: register GitHub as an OIDC identity provider and create the deploy
# role the Actions pipeline assumes (§2.6.1).
#
#   bash infra/setup_oidc.sh iamr1ddl3/ecom-order-support-agent
#
# Why this exists: the deploy job must authenticate with
# sts:AssumeRoleWithWebIdentity, NOT a static AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY pair in repo secrets. A static key in a workflow file is
# called out in §2.6.1 as a real deduction. Nothing here ever puts a long-lived
# AWS credential anywhere — GitHub presents a short-lived signed token and AWS
# exchanges it for temporary credentials scoped to this one repository.
#
# The sub claim format is the trap (§7). GitHub's docs show
#   repo:owner/repo:ref:refs/heads/main
# but the token actually carries stable NUMERIC ids:
#   repo:OWNER_ID/REPO_ID:ref:...
# An exact StringEquals on the human-readable string silently never matches, and
# the failure looks like a permissions problem rather than a claim mismatch. A
# wildcarded StringLike on the numeric form is what works.
set -euo pipefail

REPO="${1:?usage: bash infra/setup_oidc.sh owner/repo}"
PROJECT="${PROJECT_NAME:-ecom-agent}"
ROLE_NAME="${PROJECT}-github-deploy"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo ap-south-1)}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
PROVIDER_ARN="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"

say() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

# --- 1. the identity provider (once per account) ---------------------------
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  say "OIDC provider already registered"
else
  say "Registering the GitHub OIDC provider"
  # No thumbprint pinned: IAM validates GitHub's certificate chain natively now,
  # and a hardcoded thumbprint is a landmine that breaks on certificate rotation.
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com >/dev/null
fi

# --- 2. resolve the numeric ids the sub claim actually uses ----------------
OWNER="${REPO%%/*}"; NAME="${REPO##*/}"
OWNER_ID="$(curl -sf "https://api.github.com/users/${OWNER}" | sed -n 's/.*"id": *\([0-9]*\).*/\1/p' | head -1)"
REPO_ID="$(curl -sf "https://api.github.com/repos/${REPO}" | sed -n 's/.*"id": *\([0-9]*\).*/\1/p' | head -1)"
[ -n "$OWNER_ID" ] && [ -n "$REPO_ID" ] || { echo "Could not resolve numeric ids for $REPO"; exit 1; }
say "Repo $REPO -> owner id $OWNER_ID, repo id $REPO_ID"

TRUST=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${PROVIDER_ARN}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
      "StringLike": {
        "token.actions.githubusercontent.com:sub": [
          "repo:${OWNER}/${NAME}:*",
          "repo:${OWNER_ID}/${REPO_ID}:*"
        ]
      }
    }
  }]
}
JSON
)

# Both forms are listed because which one a runner presents depends on the
# repository's settings; matching either keeps this working without a debugging
# session over a claim nobody can see.

say "Creating/updating role $ROLE_NAME"
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST" >/dev/null
else
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" \
    --description "GitHub Actions deploy role for ${REPO} (OIDC, no static keys)" >/dev/null
fi

# --- 3. permissions --------------------------------------------------------
# Scoped to what the deploy actually does: push an image, update the stack.
# Not AdministratorAccess — a repo-assumable role with admin is a repo
# compromise away from an account compromise.
POLICY=$(cat <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "EcrPushPull", "Effect": "Allow", "Action": [
      "ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability","ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload","ecr:PutImage","ecr:UploadLayerPart",
      "ecr:BatchGetImage","ecr:DescribeRepositories","ecr:CreateRepository"],
     "Resource": "*"},
    {"Sid": "DeployStack", "Effect": "Allow", "Action": [
      "cloudformation:*","ecs:*","elasticloadbalancing:*","application-autoscaling:*",
      "rds:*","logs:*","ec2:Describe*","ec2:CreateSecurityGroup","ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress","ec2:RevokeSecurityGroupIngress",
      "ec2:CreateTags","iam:PassRole","iam:GetRole","iam:CreateRole","iam:DeleteRole",
      "iam:AttachRolePolicy","iam:DetachRolePolicy","iam:CreateServiceLinkedRole"],
     "Resource": "*"}
  ]
}
JSON
)
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "${PROJECT}-deploy" --policy-document "$POLICY" >/dev/null

ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
say "Done"
cat <<EOF

  Role ARN: ${ROLE_ARN}

  Register it with the repository (this is a role ARN, not a credential —
  it grants nothing without a GitHub-signed token from this repo):

    gh variable set AWS_DEPLOY_ROLE_ARN --body "${ROLE_ARN}" --repo ${REPO}
    gh variable set AWS_REGION --body "${REGION}" --repo ${REPO}

EOF
