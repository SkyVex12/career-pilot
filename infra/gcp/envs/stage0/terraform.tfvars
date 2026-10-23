# infra/gcp/envs/stage0/terraform.tfvars
#
# Stage 0 on GCP (ARCHITECTURE.md, "Stage 0 on GCP"): one VM runs every container, Postgres,
# Valkey and ElasticMQ on its data disk, no load balancer, no TLS, port 80 behind an IP
# allow-list. Two lists are not in git: put them in allowlist.auto.tfvars (gitignored) as
#   allowed_cidrs  = ["203.0.113.7/32"]
#   allowed_emails = ["you@gmail.com"]

name            = "copilot-stage0"
project_id      = "career-pilot-512115"
project_number  = "869820620165"
billing_account = "01E781-540C4F-0EF7E4"
region          = "us-central1" # Iowa: central to the team (US Central time), OpenAI and Deepgram
zone            = "us-central1-a"
copilot_env     = "staging"

# 2 shared vCPUs, 2 GiB; the services measured about 1 GiB in all. x86, so the images are
# built for linux/amd64 (and linux/arm64, for AWS). A t2a-standard-1 (ARM, 4 GiB) costs twice
# as much.
machine_type = "e2-small"
boot_image   = "projects/cos-cloud/global/images/family/cos-stable"
boot_disk_gb = 10
data_disk_gb = 10

postgres_image  = "pgvector/pgvector:0.8.7-pg16" # as in docker-compose.yml
valkey_image    = "valkey/valkey:8.1-alpine"
elasticmq_image = "softwaremill/elasticmq-native:1.7.1"

# The Desktop app OAuth client from the Console (README.md, "Deploying Stage 0"); its secret
# goes into Secret Manager, not here.
google_oauth_client_id = "869820620165-h5j6m5ietjjb2d3ipfvfj7kft4mkei14.apps.googleusercontent.com"

# The models of the team's local .env (October 2026): the 2026-10-07 live test's, with memory
# extraction on gpt-6-sol and low reasoning for the background jobs.
openai_models = {
  detector = "gpt-6-luna"
  fast     = "gpt-6-luna"
  deep     = "gpt-6.1-sol"
  extract  = "gpt-6-sol"
  job      = "gpt-6.1-sol"
  pack     = "gpt-6.1-sol"
  postcall = "gpt-6.1-sol"
  embed    = "text-embedding-3-large"
}
openai_reasoning = {
  detector = "none"
  fast     = "none"
  deep     = "low"
  extract  = "low"
  job      = "low"
  pack     = "low"
  postcall = "low"
}
openai_first_token_timeout_ms      = 3000
openai_deep_first_token_timeout_ms = 6000
model_prices = {
  "gpt-6-luna"  = { input = 0.1, cached = 0.01, output = 0.5 }
  "gpt-6.1-sol" = { input = 2, cached = 0.1, output = 10 }
  "gpt-6-astra" = { input = 10, cached = 1, output = 50 }
}

scheduler_minutes  = 5
backup_time_utc    = "08:30:00" # 3:30 in the morning, US Central
backup_days        = 7
image_keep_count   = 30 # about 10 deploys: each multi-arch tag is three versions
budget_monthly_usd = 25
budget_emails      = ["leadstar116@proton.me"]
github_repo        = "SkyVex12/career-pilot"

# The Career-OS backend (Cloud Run, project-045de1e9-8f62-42ef-b6d, us-east1), for linking
# accounts and pulling applications (ARCHITECTURE.md, "Career-OS link"). Its web app is
# https://career-os-delta.vercel.app, where users make the link code.
career_os_url = "https://career-os-backend-27mjonenmq-ue.a.run.app"
