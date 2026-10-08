# infra/envs/stage0/terraform.tfvars
#
# Stage 0 as decided for build step 5: one ARM64 instance runs every container (ECS on EC2),
# with Postgres and Valkey on it, no load balancer, no TLS, port 80 behind an IP allow-list.
# This replaces the Fargate / ALB / Aurora / ElastiCache settings in ARCHITECTURE.md's
# "Stage 0 settings"; see the note there. The allow-list is not in git: put it in
# allowlist.auto.tfvars (gitignored) as  allowed_cidrs = ["203.0.113.7/32"].

name        = "copilot-stage0"
region      = "us-east-1"
copilot_env = "staging"

az_count       = 1
instance_type  = "t4g.small" # 2 GiB; the services measured about 1 GiB in all
data_volume_gb = 10
snapshot_days  = 7

postgres_image = "pgvector/pgvector:0.8.7-pg16" # as in docker-compose.yml
valkey_image   = "public.ecr.aws/docker/library/valkey:8.1-alpine"

cognito_domain_prefix = "career-os-copilot-stage0"

# The same models as the 2026-10-07 live test (.env.example).
openai_models = {
  detector = "gpt-6-luna"
  fast     = "gpt-6-luna"
  deep     = "gpt-6.1-sol"
  extract  = "gpt-6-astra"
  job      = "gpt-6.1-sol"
  pack     = "gpt-6.1-sol"
  postcall = "gpt-6.1-sol"
  embed    = "text-embedding-3-large"
}
openai_reasoning = {
  detector = "none"
  fast     = "none"
  deep     = "low"
  extract  = "medium"
  job      = "low"
  pack     = "medium"
  postcall = "medium"
}
openai_first_token_timeout_ms      = 3000
openai_deep_first_token_timeout_ms = 6000
model_prices = {
  "gpt-6-luna"  = { input = 0.1, cached = 0.01, output = 0.5 }
  "gpt-6.1-sol" = { input = 2, cached = 0.1, output = 10 }
  "gpt-6-astra" = { input = 10, cached = 1, output = 50 }
}

log_retention_days = 7
xray_sampling_rate = 0.05
budget_monthly_usd = 25
budget_emails      = ["skyvex12@outlook.com"]
github_repo        = "SkyVex12/career-pilot"
scheduler_rate     = "rate(5 minutes)"
