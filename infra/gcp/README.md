# infra/gcp

Terraform for Stage 0 on Google Cloud (project `career-pilot-512115`, us-central1). The design is
in ARCHITECTURE.md ("Stage 0 on GCP"); setting up, deploying, rolling back, logs, backups and
teardown are in the main README, "Deploying Stage 0". This file covers the layout and the
maintenance steps that come up rarely. The AWS Terraform (`infra/envs`, `infra/modules`) stays
in the repository, unused.

```
bootstrap/        the Terraform state bucket (local state; once per project)
envs/stage0/      Stage 0: main.tf wires everything, terraform.tfvars sizes it;
                  allowlist.auto.tfvars (not in git) holds the IPs and the sign-in emails
modules/
  host/           the VM, its static IP, firewall rules (port 80, IAP SSH), the data disk, its
                  service account, and the boot config (cloud-init.yaml.tftpl)
  ci/             Workload Identity Federation for GitHub Actions and its three accounts:
                  ci (image pushes), deploy (start the VM, IAP + OS Login), release (the
                  desktop releases bucket)
host/             files the VM gets at every boot: copilotctl.sh, elasticmq.conf
scripts/stage.py  one command per operation (deploy, rollback, health, logs, backups,
                  allow-list, secrets, start and stop, desktop-config); it runs copilotctl on
                  the VM over IAP SSH
```

## On the VM

Container-Optimized OS: Docker, systemd and a logging agent, no package manager, `/etc` in
memory. cloud-init writes `copilotctl` and the systemd units at every boot and runs
`copilotctl boot`, which mounts the data disk at `/mnt/disks/data` (formatting it the first
time), opens port 80 in the host firewall, adds 1 GiB of swap and starts the containers. Each
container is a unit, `copilot-<name>`, whose start command is `copilotctl run <name>`: it reads
the workload's environment from the instance metadata (`copilot-env`), fetches the secrets it
names (`{{secret:<id>}}`) from Secret Manager into `/run/copilot/<name>.env` (memory, root only),
and runs the container with host networking.

| Workload | Image | Port (127.0.0.1) | Memory limit | uid |
| --- | --- | --- | --- | --- |
| proxy (nginx) | registry `proxy` | 80 (all addresses) | 96 MiB | nginx's |
| gateway | registry `gateway` | 8081 | 512 MiB | 10003 |
| api | registry `api` | 8080 | 384 MiB | 10001 |
| worker | registry `worker` | 8082 (health) | 640 MiB | 10002 |
| postgres | `pgvector/pgvector:0.8.7-pg16` | 5432 | 512 MiB | postgres's |
| valkey | `valkey/valkey:8.1-alpine` | 6379 | 320 MiB | valkey's |
| elasticmq | `softwaremill/elasticmq-native:1.7.1` | 9324 | 128 MiB | 10005 |
| migrate (one-off, every deploy) | registry `worker` | | 384 MiB | 10004 |
| scheduler (one-off, timer every 5 min) | registry `worker` | | 256 MiB | 10004 |
| backup (timer, 08:30 UTC) | `pg_dump` in the postgres container | | | |

The data disk holds `postgres/`, `valkey/`, `elasticmq/` (the jobs queue and its DLQ, in an H2
file), `copilot/` (the deployed tag and the earlier ones, for rollbacks), `backups/` (the newest
dump) and the swap file.

**Identity on one host.** The VM has one service account; containers share the host's
network, so the host firewall lets only root, the API (uid 10001) and the workers (10002) reach
the metadata server, which hands out its tokens. The API and the workers need it for Cloud KMS;
the gateway, the one-off tasks, ElasticMQ and the images' own users cannot reach it. Secrets
reach the containers only through their env files. On AWS each task had its own role; here the
API and the workers share the VM's, which may use the KMS key, read the stage's secrets and the
registry, write logs and use the backups bucket.

## The uploads HMAC key

The API and the workers use Cloud Storage through its S3-compatible XML API (presigned uploads
included), signed with an HMAC key of the `copilot-stage0-uploads` service account, which may
only use the uploads bucket. The organization enforces
`iam.disableServiceAccountKeyCreation`, Google's default for new organizations, and that covers
HMAC keys of service accounts. `stage.py secrets generate --lift-key-policy` therefore grants the
signed-in Organization Administrator Org Policy Administrator, turns the constraint off for this
project only, creates the key straight into Secret Manager, deletes the project's override (so
the organization's enforcement applies again) and removes the grant. A key keeps working after
the policy is back; the constraint blocks only creating new ones.

To rotate it: `gcloud storage hmac list`, then `gcloud storage hmac update <access id>
--deactivate` and `gcloud storage hmac delete <access id>`, empty both `uploads-hmac-*`
secrets' versions (`gcloud secrets versions disable`), rerun the generate command and
`stage.py restart`.

## Rotating the database passwords

Not automated. Postgres keeps the superuser password it was initialized with, so: `stage.py
ssh`, `sudo docker exec -it copilot-postgres psql -U postgres -d copilot`, `alter role postgres
password '<new>'`; put the same value in `copilot-stage0-db-postgres` (`stage.py secrets set
db-postgres`); put new values in the `db-app-*` secrets the same way; then `stage.py deploy --tag
<current tag>`, whose migration step sets the app roles' passwords from the secrets and restarts
the services.

## Updating Container-Optimized OS

The boot image is ignored after creation, so the VM is never replaced by surprise. COS updates
itself in place from its channel (stable). To move to a new image deliberately:

```sh
terraform -chdir=infra/gcp/envs/stage0 apply -replace=module.host.google_compute_instance.vm
```

The data disk moves to the new VM; the app is down for about five minutes. The IP stays.

## Changing the machine type

`machine_type` in `envs/stage0/terraform.tfvars`, then apply: the VM stops, changes and starts
again (a few minutes). An e2-medium (4 GiB) doubles the VM's cost.

## The KMS key ring after a teardown

Google never deletes key rings. After `terraform destroy` the ring `copilot-stage0` stays,
empty and free; a new apply in the same project fails on it until you either import it
(`terraform import google_kms_key_ring.copilot projects/career-pilot-512115/locations/us-central1/keyRings/copilot-stage0`)
or change `name`. Data encrypted under the destroyed key versions cannot be read again.
