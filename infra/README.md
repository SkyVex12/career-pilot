# infra

Terraform for AWS, kept but unused: Stage 0 runs on Google Cloud (`infra/gcp`, with its own
README), because the AWS account's organization policy blocks it. The design is in
ARCHITECTURE.md ("Infrastructure on AWS", "Stage 0 settings", "Terraform layout and IAM");
setting up, deploying, rolling back, logs and teardown on AWS are at the end of this file,
"Deploying Stage 0 on AWS (unused)".

```
bootstrap/        the Terraform state bucket (local state; once per account)
envs/stage0/      Stage 0: main.tf wires the modules, terraform.tfvars sizes them
modules/
  network/        VPC, one public subnet, S3 gateway endpoint (no NAT, no load balancer)
  host/           the instance, its Elastic IP and security group, the data volume and its
                  daily snapshots, the boot script (ECS agent, IMDS guard, swap, data mount)
  ecs-cluster/    the cluster the instance joins
  service/        one workload on the instance: task definition, log group, execution role,
                  task role, and the ECS service (or a one-off task)
  ecr/  sqs/  s3/  kms/  cognito/  secrets/  observability/  ci/
scripts/deploy.py migrations, then the services, on an image tag (CI and rollbacks)
```

Workloads on the instance, all with host networking, each listening on 127.0.0.1 except the
proxy:

| Workload | Image | Port | Memory (reserved / limit, MiB) |
| --- | --- | --- | --- |
| proxy (nginx) | ECR `proxy` | 80 | 32 / 96 |
| gateway | ECR `gateway` | 8081 | 192 / 512 |
| api | ECR `api` | 8080 | 128 / 384 |
| worker | ECR `worker` | 8082 (health) | 160 / 640 |
| postgres | `pgvector/pgvector:0.8.7-pg16` | 5432 | 256 / 512 |
| valkey | `public.ecr.aws/valkey/valkey:8.1-alpine` | 6379 | 64 / 320 |
| migrate (one-off) | ECR `worker` | | 96 / 384 |
| scheduler (one-off, every 5 min) | ECR `worker` | | 96 / 256 |

## Updating the instance's AMI

Terraform ignores new ECS-optimized AMIs, so the instance is never replaced by surprise. To move
to the current one (the data volume moves with it; Postgres and Valkey keep their data):

```sh
terraform -chdir=infra/envs/stage0 apply -replace=module.host.aws_instance.host
```

The app is down for about five minutes while the new instance boots, mounts the volume and
starts the services.

## Rotating the database passwords

Not automated in Stage 0. The passwords are generated once (ephemeral, write-only: they are in
Secrets Manager and nowhere else). A new `database_secret_version` for `modules/secrets` writes
new ones; a deploy afterwards (`deploy.py --tag <current tag>`) sets the app roles' passwords
from them and restarts the services. Postgres keeps the superuser password it was initialized
with, so the superuser's must be changed in Postgres by hand to match (ECS Exec into the
postgres task, `alter role postgres password ...`) before that deploy.

## Changing the instance size

`instance_type` in `envs/stage0/terraform.tfvars`, then apply: the instance stops, changes type
and starts again (a few minutes of downtime). A t4g.medium doubles the instance's cost.

## Deploying Stage 0 on AWS (unused)

The steps as written for AWS (PR #8). Stage 0 runs on GCP now (`infra/gcp`, README.md
"Deploying Stage 0"): the AWS account's organization policy denies EC2, SQS, Cognito and most
creates. They apply again if that changes; the desktop app's Cloud environment works with
Cognito too.

Stage 0 runs in one AWS account in us-east-1 on a single ARM64 instance: gateway, API, worker,
an nginx proxy, Postgres with pgvector and Valkey run as containers through ECS on EC2, next
to SQS, S3, Cognito, KMS, Secrets Manager and CloudWatch Logs (ARCHITECTURE.md, "Stage 0
settings"; the layout is above). It costs about $22 a month running all the time
and about $10 stopped between tests, plus OpenAI and Deepgram per use.

**No TLS in Stage 0.** The app talks to the instance over plain HTTP and WebSocket on its AWS
host name, and the instance admits port 80 only from an IP allow-list. Your sign-in token and
call text cross the internet unencrypted on that hop; the settings screen says so. TLS
(CloudFront or a real domain) is required before anyone outside the team uses it.

You need the AWS CLI, Terraform 1.11+ (`winget install Amazon.AWSCLI Hashicorp.Terraform`),
`gh`, and `uv`.

#### First-time setup

Done once, by hand, in this order.

1. **Sign in to AWS** in a terminal: `aws login` (or `aws configure sso`), region `us-east-1`.
   `aws sts get-caller-identity` shows the account. The account must allow EC2, ECS, Cognito,
   SQS, S3, KMS, Secrets Manager, ECR, EventBridge Scheduler, DLM, X-Ray and IAM (an
   organization's service control policy can block them).
2. **State bucket**, once per account:

   ```sh
   terraform -chdir=infra/bootstrap init
   terraform -chdir=infra/bootstrap apply          # creates copilot-tfstate-<account id>
   ```

3. **Allow-list.** In `infra/envs/stage0/allowlist.auto.tfvars` (gitignored), one /32 per
   teammate who will connect (`curl https://checkip.amazonaws.com` shows yours):

   ```hcl
   allowed_cidrs = ["203.0.113.7/32", "198.51.100.20/32"]
   ```

4. **The stage.** Review the plan before applying:

   ```sh
   cd infra/envs/stage0
   terraform init -backend-config="bucket=copilot-tfstate-<account id>"
   terraform plan -out=tfplan
   terraform apply tfplan
   terraform output                                   # URLs, Cognito, secrets, roles
   ```

   The app services wait at zero tasks until the first deploy; Postgres and Valkey start
   right away.
5. **Secrets.** Put the two API keys in (never in a file or a commit; the prompt keeps them out
   of your shell history):

   ```sh
   aws secretsmanager put-secret-value --secret-id copilot-stage0/openai-api-key --secret-string "$(read -rsp 'OpenAI key: ' k; echo "$k")"
   aws secretsmanager put-secret-value --secret-id copilot-stage0/deepgram-api-key --secret-string "$(read -rsp 'Deepgram key: ' k; echo "$k")"
   ```

   That is Git Bash; in PowerShell 7 use `--secret-string (Read-Host -MaskInput 'OpenAI key')`.
   The database passwords are generated by Terraform straight into
   `copilot-stage0/database`; they are not in the Terraform state.
6. **Users.** Sign-up is closed; create each teammate (Cognito emails them a temporary password,
   and they choose their own at the first sign-in):

   ```sh
   aws cognito-idp admin-create-user --user-pool-id <cognito.user_pool_id> \
     --username alex@example.com --user-attributes Name=email,Value=alex@example.com Name=email_verified,Value=true
   ```

7. **CI.** Repository variables for the workflows (Settings → Secrets and variables → Actions →
   Variables, or `gh variable set`), from `terraform output`:

   | Variable | Value |
   | --- | --- |
   | `AWS_PLAN_ROLE_ARN` | `ci_roles.plan` |
   | `AWS_DEPLOY_ROLE_ARN` | `ci_roles.deploy` |
   | `TF_STATE_BUCKET` | `copilot-tfstate-<account id>` |
   | `TF_ALLOWED_CIDRS` | the allow-list as JSON, e.g. `["203.0.113.7/32"]` (for the PR plan) |

   No AWS keys are stored in GitHub: both workflows assume their role through GitHub's OIDC
   provider. The deploy role trusts pushes to `main` only.
8. **First deploy.** Run the `deploy` workflow from the Actions tab (on `main`), or merge a PR.
   It builds the images on GitHub's ARM runners, pushes them, runs the migrations and starts
   the services. Then `terraform apply` once more, so Terraform records the deployed images.
9. **DNS: nothing to do.** Stage 0 uses the Elastic IP's AWS host name (`terraform output
   public_dns`); Cognito uses its own `amazoncognito.com` domain.
10. **The desktop app.** Settings → Environment: Cloud. Paste `public_dns` into "Cloud host" and
    press Fill URLs (or paste `api_url` and `gateway_url`), Save, then Sign in: the browser
    opens Cognito's page, and the app keeps the sign-in in the Windows Credential Manager.

#### Deploy

Merging to `main` deploys (`.github/workflows/deploy.yml`): ARM64 images tagged with the git
SHA, the migration task, then each service. On the one instance each service's old task stops
before the new one starts, so a call in progress reconnects and resumes in a few seconds. The
instance must be running; a deploy to a stopped stage fails and can be rerun once it is up.

By hand, with images already in ECR:

```sh
uv run --no-project --with boto3 python infra/scripts/deploy.py --tag <git sha>
uv run --no-project --with boto3 python infra/scripts/deploy.py --status
```

Infrastructure changes are not deployed by CI: each PR shows its `terraform plan` as a comment,
and someone runs `terraform apply` after merging.

#### Rollback

Deploy the previous tag; `deploy.py` prints it at the end of every deploy (`rollback: --tag
…`), and ECR keeps the last 15 images:

```sh
uv run --no-project --with boto3 python infra/scripts/deploy.py --tag <previous sha>
```

or run the `deploy` workflow with that tag. Migrations follow expand then contract, so the
older code runs against the newer schema. A failed deploy that never got healthy rolls itself
back (the ECS deployment circuit breaker).

#### Logs

Every service logs to CloudWatch Logs, one group per service (`/copilot-stage0/gateway`, `api`,
`worker`, `scheduler`, `migrate`, `proxy`, `postgres`, `valkey`, kept 7 days). The Python
services write JSON lines, and every line about a session carries its `session_id`.

- **One session, every service:** CloudWatch → Logs Insights → Saved queries →
  `copilot-stage0/one session`; put the session id in the filter and run it over the session's
  time range. The id is in the app's history and in `answers`/`sessions`.
- **Follow a service live:** `aws logs tail /copilot-stage0/gateway --follow`
  (`--filter-pattern '{ $.session_id = "<id>" }'` for one session).
- **Errors:** the saved query `copilot-stage0/errors`.
- **A shell on the instance:** `aws ssm start-session --target <instance_id>` (no SSH);
  inside a task: `aws ecs execute-command --cluster copilot-stage0 --task <task id> --container
  api --interactive --command sh`.

#### Stopping between tests

Stopping the instance stops the whole app; only the volumes, the Elastic IP and the small fixed
items keep billing (about $10 a month).

```sh
aws ec2 stop-instances  --instance-ids <instance_id>
aws ec2 start-instances --instance-ids <instance_id>   # the services start again in 2-3 minutes
```

While it is stopped, the scheduler's 5-minute runs fail and are dropped, and deploys fail.

#### Tearing it all down

```sh
terraform -chdir=infra/envs/stage0 destroy
terraform -chdir=infra/bootstrap destroy        # the state bucket, last
```

This deletes the instance, the data, the images, the uploads, the user pool and the secrets.
Two things are left on purpose: a final snapshot of the data volume (delete it in EC2 →
Snapshots when you no longer want the data; a few cents a month) and the KMS key, which AWS
deletes after its 7-day waiting period. Remove the repository variables so the workflows skip
the AWS steps again.
