# infra

Terraform for AWS. The design is in ARCHITECTURE.md ("Infrastructure on AWS", "Stage 0
settings", "Terraform layout and IAM"); setting up, deploying, rolling back, logs and teardown
are in the main README, "Deploying Stage 0". This file covers the layout and the maintenance
steps that come up rarely.

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
