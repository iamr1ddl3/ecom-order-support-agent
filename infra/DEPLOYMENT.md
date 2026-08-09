# AWS deployment — live evidence (§2.6)

Real output from the running stack, captured during deployment. §6 flags
"describing the AWS deployment instead of demonstrating it" as this assignment's
new pitfall, so everything here is a command that ran and what it returned.

## Resource names (§8.6)

| Thing | Value |
|---|---|
| Region / Account | `ap-south-1` / `289702314533` |
| ALB URL | `http://ecom-agent-alb-81067588.ap-south-1.elb.amazonaws.com` |
| ECS cluster | `ecom-agent-cluster` |
| ECS service | `ecom-agent-service` |
| Task definition | `ecom-agent-task` (Fargate, 512 CPU / 1024 MiB) |
| RDS instance | `ecom-agent-db` (`db.t4g.micro`, postgres 17.5, single-AZ) |
| RDS endpoint | `ecom-agent-db.cx6uceywqsdj.ap-south-1.rds.amazonaws.com` |
| ECR image | `289702314533.dkr.ecr.ap-south-1.amazonaws.com/ecom-agent:<git-sha>` |
| Autoscaling | CPU target-tracking, **50%**, min **1** / max **4** |
| OIDC deploy role | `arn:aws:iam::289702314533:role/ecom-agent-github-deploy` |

## The agent responding at its real ALB URL (§4.5)

**RAG grounded in RDS pgvector** — retrieval runs against Postgres in RDS, not a
local store:

```console
$ curl -X POST $URL/chat -H 'content-type: application/json' \
    -d '{"customer_id":"cust_1001","message":"How many days after delivery do I have to request a refund?"}'
{
  "reply": "According to our refund policy, you have **30 days from the delivery date**
            to request a refund for eligible items...",
  "trajectory": ["retrieval:refund_window"],
  "gate_decisions": []
}
```

**Tool call** — the MCP server runs as a stdio subprocess *inside* the Fargate task:

```console
$ curl -X POST $URL/chat -d '{"customer_id":"cust_1001","message":"What is the status of my order ord_5001?"}'
{
  "reply": "Your order ord_5001 for Wireless Earbuds has been **shipped**...",
  "trajectory": ["tool:lookup_order"],
  "gate_decisions": [{"tool": "lookup_order", "allowed": true, "reason": "allowed"}]
}
```

**The permission gate, denying a cross-customer read in production:**

```console
$ curl -X POST $URL/chat -d '{"customer_id":"cust_1001","message":"Can you look up order ord_6002 for me?"}'
{
  "reply": "I'm sorry, but I cannot look up order ord_6002 for you. The system indicates
            that this order belongs to a different customer (cust_2002)...",
  "trajectory": ["tool:lookup_order"],
  "gate_decisions": [{
    "tool": "lookup_order", "allowed": false,
    "reason": "order 'ord_6002' belongs to cust_2002, not ticket customer cust_1001 — rejected"
  }]
}
```

## A real scale-out event (§2.6.3)

`bash infra/load_test.sh $URL` — 12 concurrent workers against `/chat`. It hits
`/chat` rather than `/health` deliberately: `/health` returns a dict and burns no
CPU, so the metric the policy tracks would never move and the demo would prove
nothing.

```
TIME      RUNNING  DESIRED  CPU%
1s        1        1        12.27
64s       1        1        35.35
127s      1        1        99.98      <- saturated, well past the 50% target
252s      1        1        100.0
394s      1        2        99.99      <- SCALED OUT
409s      1        2        99.99
```

AWS's own record of the decision:

```console
$ aws application-autoscaling describe-scaling-activities \
    --service-namespace ecs --resource-id service/ecom-agent-cluster/ecom-agent-service
{
  "cause": "monitor alarm TargetTracking-.../ecom-agent-service-AlarmHigh-c057bb4f... in state ALARM
            triggered policy ecom-agent-cpu-target-tracking",
  "status": "Successfully set desired count to 2. Change successfully fulfilled by ecs.",
  "start": "2026-08-09T16:12:58+05:30"
}
```

The ~6-minute lag from saturation to scale-out is the CloudWatch alarm requiring
three consecutive breaching datapoints at 1-minute periods — expected behaviour,
not a misconfiguration, and worth knowing before recording: leave the load
running past the 6-minute mark or the clip ends before the event.

## Four bugs the real deploy exposed

None of these were visible locally. Recorded because they're the actual content
of "we deployed it" versus "we wrote a template".

**1. SIGPIPE killed the deploy script silently.**
`tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24` — `head` closes the pipe at 24
bytes, `tr` gets SIGPIPE, and `set -o pipefail` turns that into exit 141. The
script died immediately after writing a perfectly valid password file, with no
error message. Replaced with `openssl rand`.

**2. ECR couldn't be in the template.**
The image must exist before the service can start, so `deploy.sh` creates the
repo and pushes first — and a template that *also* declares it is rejected with
`AWS::EarlyValidation::ResourceExistenceCheck`. Chicken-and-egg, only visible on
a real deploy. ECR is now owned by the script and removed by `teardown.sh`.

**3. `EnableExecuteCommand` doesn't apply to running tasks.**
RDS is deliberately unreachable from outside the VPC, so the index has to be
seeded from inside the task via ECS Exec. Setting the flag updates the *service*,
but existing tasks keep `enableExecuteCommand: false` — a `--force-new-deployment`
is required before Exec works.

**4. One failed query poisoned the connection permanently.**
The most serious of the four. Before the index existed, the first query failed on
a missing table — and every subsequent query returned *"current transaction is
aborted, commands ignored until end of transaction block"*. The connection is
one-per-process and psycopg leaves the failed transaction open, so a single
transient error would take retrieval down until the task restarted, with the
original cause long gone and every trace pointing at the wrong thing. Now rolled
back on failure, with the connection discarded if the rollback itself fails.
Covered by `rag/test_pgvector_recovery.py` — no database needed, runs in CI.

## Reproduce

```bash
bash infra/setup_oidc.sh <owner>/<repo>   # once per account
bash infra/deploy.sh                      # nothing -> live URL, ~10 min
bash infra/seed_index.sh                  # if deploy.sh couldn't seed
bash infra/load_test.sh <alb-url>         # scale-out demo
bash infra/teardown.sh                    # remove everything
```
