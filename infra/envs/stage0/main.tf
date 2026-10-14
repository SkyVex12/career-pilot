# Stage 0 on AWS (build step 5): every container on one ARM64 instance through ECS on EC2,
# managed services only where they cost almost nothing (SQS, S3, Cognito, KMS, Secrets
# Manager, CloudWatch Logs, EventBridge Scheduler). See README.md, "Deploying Stage 0", and infra/README.md.

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  prefix  = var.name

  # Deployed by CI (infra/scripts/deploy.py); "none" until the first deploy, when the app
  # services wait at zero tasks.
  image_tag = nonsensitive(data.aws_ssm_parameter.image_tag.value)
  deployed  = local.image_tag != "none"
  app_count = local.deployed ? 1 : 0
  image     = { for k, url in module.ecr.urls : k => "${url}:${local.image_tag}" }

  public_dns  = module.host.public_dns
  api_url     = "http://${local.public_dns}/v1"
  gateway_url = "ws://${local.public_dns}/v1/session"

  db = module.secrets.database_arn

  # Every Python service.
  common_env = {
    AWS_REGION                   = var.region
    COPILOT_ENV                  = var.copilot_env
    COPILOT_LOG_FORMAT           = "json"
    COPILOT_VALKEY_URL           = "redis://127.0.0.1:6379/0"
    COPILOT_SQS_ENDPOINT         = "" # the real SQS, not ElasticMQ
    COPILOT_SQS_QUEUE_URL        = module.sqs.jobs_url
    COPILOT_AUTH                 = "cognito"
    COPILOT_COGNITO_USER_POOL_ID = module.cognito.user_pool_id
    COPILOT_COGNITO_CLIENT_IDS   = module.cognito.client_id
    COPILOT_COGNITO_DOMAIN       = module.cognito.domain_url
  }
  # Models: OpenAI only (COPILOT_LLM_PROVIDERS=openai); Bedrock can be added later.
  model_env = merge(
    {
      COPILOT_LLM_PROVIDERS                      = "openai"
      COPILOT_EMBEDDER                           = "openai"
      COPILOT_OPENAI_EMBED_MODEL                 = var.openai_models["embed"]
      COPILOT_OPENAI_FIRST_TOKEN_TIMEOUT_MS      = tostring(var.openai_first_token_timeout_ms)
      COPILOT_OPENAI_DEEP_FIRST_TOKEN_TIMEOUT_MS = tostring(var.openai_deep_first_token_timeout_ms)
      COPILOT_MODEL_PRICES                       = jsonencode(var.model_prices)
    },
    { for job, model in var.openai_models : "COPILOT_OPENAI_${upper(job)}_MODEL" => model if job != "embed" },
    { for job, effort in var.openai_reasoning : "COPILOT_OPENAI_${upper(job)}_REASONING" => effort },
  )
  # The API and the workers: S3 and KMS.
  data_env = {
    COPILOT_S3_ENDPOINT  = "" # the real S3
    COPILOT_S3_BUCKET    = module.s3.bucket
    COPILOT_KEY_PROVIDER = "kms"
    COPILOT_KMS_KEY_ID   = module.kms.alias
  }

  python_health = { for svc, port in { gateway = 8081, api = 8080, worker = 8082 } : svc => [
    "CMD-SHELL",
    "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${port}/healthz', timeout=3)\" || exit 1",
  ] }
}

# --- Foundations ---------------------------------------------------------------------------------

module "network" {
  source   = "../../modules/network"
  name     = local.prefix
  az_count = var.az_count
}

module "ecr" {
  source       = "../../modules/ecr"
  name         = local.prefix
  repositories = ["gateway", "api", "worker", "proxy"]
}

module "kms" {
  source = "../../modules/kms"
  name   = local.prefix
}

module "sqs" {
  source = "../../modules/sqs"
  name   = local.prefix
}

module "s3" {
  source = "../../modules/s3"
  name   = local.prefix
}

module "cognito" {
  source        = "../../modules/cognito"
  name          = local.prefix
  domain_prefix = var.cognito_domain_prefix
}

module "secrets" {
  source = "../../modules/secrets"
  name   = local.prefix
}

module "cluster" {
  source = "../../modules/ecs-cluster"
  name   = local.prefix
}

module "host" {
  source         = "../../modules/host"
  name           = local.prefix
  vpc_id         = module.network.vpc_id
  subnet_id      = module.network.public_subnet_ids[0]
  cluster_name   = module.cluster.name
  instance_type  = var.instance_type
  allowed_cidrs  = var.allowed_cidrs
  data_volume_gb = var.data_volume_gb
  snapshot_days  = var.snapshot_days
}

resource "aws_ssm_parameter" "image_tag" {
  name        = "/${local.prefix}/image-tag"
  description = "The git SHA deployed to the stage (written by infra/scripts/deploy.py)"
  type        = "String"
  value       = "none"
  lifecycle {
    ignore_changes = [value]
  }
}

data "aws_ssm_parameter" "image_tag" {
  name = aws_ssm_parameter.image_tag.name
}

# --- Task role policies (ARCHITECTURE.md, "Terraform layout and IAM": the role table) ----------

data "aws_iam_policy_document" "send_jobs" {
  statement {
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
    resources = [module.sqs.jobs_arn]
  }
}

data "aws_iam_policy_document" "user_keys" {
  statement {
    sid       = "UserDataKeys"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [module.kms.key_arn]
    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:user_id"
      values   = ["?*"]
    }
  }
}

data "aws_iam_policy_document" "api" {
  source_policy_documents = [data.aws_iam_policy_document.send_jobs.json, data.aws_iam_policy_document.user_keys.json]
  statement {
    sid       = "Uploads"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${module.s3.arn}/uploads/*"]
  }
  statement {
    sid       = "ListUploads"
    actions   = ["s3:ListBucket"]
    resources = [module.s3.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["uploads/*"]
    }
  }
}

data "aws_iam_policy_document" "worker" {
  source_policy_documents = [data.aws_iam_policy_document.user_keys.json]
  statement {
    sid = "Jobs"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
      "sqs:SendMessage", "sqs:GetQueueAttributes",
    ]
    resources = [module.sqs.jobs_arn]
  }
  statement {
    sid       = "Uploads"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${module.s3.arn}/uploads/*"]
  }
  statement {
    sid       = "ListUploads"
    actions   = ["s3:ListBucket"]
    resources = [module.s3.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["uploads/*"]
    }
  }
  statement {
    sid       = "DeleteSignIn" # user.delete
    actions   = ["cognito-idp:AdminDeleteUser"]
    resources = [module.cognito.user_pool_arn]
  }
}

# --- Data services: Postgres with pgvector, and Valkey, on the data volume ---------------------

module "postgres" {
  source             = "../../modules/service"
  name               = "postgres"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = var.postgres_image
  log_retention_days = var.log_retention_days
  memory_reservation = 256
  memory             = 512
  stop_timeout       = 90 # a clean shutdown, so no recovery at the next start
  host_mounts        = { "/data/postgres" = "/pgroot" }
  command = ["sh", "-c", join(" ", [
    "test -f /pgroot/.copilot-data-volume || { echo 'data volume not mounted' >&2; exit 1; };",
    "exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c max_connections=60",
    "-c shared_buffers=128MB -c effective_cache_size=512MB -c work_mem=4MB",
  ])]
  environment = {
    POSTGRES_DB   = "copilot"
    POSTGRES_USER = "postgres"
    PGDATA        = "/pgroot/pgdata"
  }
  secrets      = { POSTGRES_PASSWORD = "${local.db}:postgres::" }
  secret_arns  = [local.db]
  health_check = ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres -d copilot"]
}

module "valkey" {
  source             = "../../modules/service"
  name               = "valkey"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = var.valkey_image
  log_retention_days = var.log_retention_days
  memory_reservation = 64
  memory             = 320
  stop_timeout       = 30
  host_mounts        = { "/data/valkey" = "/data" }
  # Session state and packs must not be evicted: noeviction makes a full Valkey refuse writes
  # (the gateway falls back to memory) instead of dropping a pack.
  command = ["sh", "-c", join(" ", [
    "test -f /data/.copilot-data-volume || { echo 'data volume not mounted' >&2; exit 1; };",
    "exec docker-entrypoint.sh valkey-server --bind 127.0.0.1 --port 6379 --protected-mode yes",
    "--dir /data --appendonly yes --appendfsync everysec --save 300 1",
    "--maxmemory 192mb --maxmemory-policy noeviction",
  ])]
  health_check = ["CMD", "valkey-cli", "-h", "127.0.0.1", "ping"]
}

# --- The app -------------------------------------------------------------------------------------

module "proxy" {
  source             = "../../modules/service"
  name               = "proxy"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = local.image["proxy"]
  desired_count      = local.app_count
  log_retention_days = var.log_retention_days
  memory_reservation = 32
  memory             = 96
  health_check       = ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1/healthz || exit 1"]
}

module "gateway" {
  source             = "../../modules/service"
  name               = "gateway"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = local.image["gateway"]
  command            = ["copilot-gateway"]
  desired_count      = local.app_count
  log_retention_days = var.log_retention_days
  memory_reservation = 192
  memory             = 512
  stop_timeout       = 30
  environment        = merge(local.common_env, local.model_env, { HOST = "127.0.0.1", PORT = "8081" })
  secrets = {
    OPENAI_API_KEY   = module.secrets.openai_arn
    DEEPGRAM_API_KEY = module.secrets.deepgram_arn
  }
  secret_arns      = [module.secrets.openai_arn, module.secrets.deepgram_arn]
  task_policy_json = data.aws_iam_policy_document.send_jobs.json
  health_check     = local.python_health["gateway"]
}

module "api" {
  source             = "../../modules/service"
  name               = "api"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = local.image["api"]
  command            = ["copilot-api"]
  desired_count      = local.app_count
  log_retention_days = var.log_retention_days
  memory_reservation = 128
  memory             = 384
  environment        = merge(local.common_env, local.data_env, { HOST = "127.0.0.1", PORT = "8080" })
  secrets            = { COPILOT_API_DATABASE_URL = "${local.db}:api_url::" }
  secret_arns        = [local.db]
  task_policy_json   = data.aws_iam_policy_document.api.json
  health_check       = local.python_health["api"]
}

module "worker" {
  source             = "../../modules/service"
  name               = "worker"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = local.image["worker"]
  command            = ["copilot-worker"]
  desired_count      = local.app_count
  log_retention_days = var.log_retention_days
  memory_reservation = 160
  memory             = 640
  stop_timeout       = 110 # lets running jobs finish (shutdown_grace_s 100)
  environment = merge(local.common_env, local.model_env, local.data_env, {
    HOST                     = "127.0.0.1"
    PORT                     = "8082"
    COPILOT_SHUTDOWN_GRACE_S = "100"
  })
  secrets = {
    COPILOT_WORKER_DATABASE_URL = "${local.db}:worker_url::"
    COPILOT_SYSTEM_DATABASE_URL = "${local.db}:system_url::"
    OPENAI_API_KEY              = module.secrets.openai_arn
    DEEPGRAM_API_KEY            = module.secrets.deepgram_arn
  }
  secret_arns      = [local.db, module.secrets.openai_arn, module.secrets.deepgram_arn]
  task_policy_json = data.aws_iam_policy_document.worker.json
  health_check     = local.python_health["worker"]
}

# One-off: migrations, then the app roles' logins from the database secret. Run before every
# deploy by infra/scripts/deploy.py.
module "migrate" {
  source             = "../../modules/service"
  name               = "migrate"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = local.image["worker"]
  command            = ["sh", "-c", "copilot-migrate && copilot-db-logins"]
  run_as_service     = false
  log_retention_days = var.log_retention_days
  memory_reservation = 96
  memory             = 384
  environment        = { AWS_REGION = var.region, COPILOT_ENV = var.copilot_env, COPILOT_LOG_FORMAT = "json" }
  secrets = {
    COPILOT_DATABASE_URL            = "${local.db}:superuser_url::"
    COPILOT_DB_PASSWORD_APP_API     = "${local.db}:app_api::"
    COPILOT_DB_PASSWORD_APP_GATEWAY = "${local.db}:app_gateway::"
    COPILOT_DB_PASSWORD_APP_WORKER  = "${local.db}:app_worker::"
    COPILOT_DB_PASSWORD_APP_SYSTEM  = "${local.db}:app_system::"
  }
  secret_arns = [local.db]
}

# One-off: one scheduler tick (ARCHITECTURE.md, "Scheduler"), run every 5 minutes below.
module "scheduler" {
  source             = "../../modules/service"
  name               = "scheduler"
  prefix             = local.prefix
  cluster_arn        = module.cluster.arn
  image              = local.image["worker"]
  command            = ["copilot-scheduler", "--once"]
  run_as_service     = false
  log_retention_days = var.log_retention_days
  memory_reservation = 96
  memory             = 256
  environment        = local.common_env
  secrets            = { COPILOT_SYSTEM_DATABASE_URL = "${local.db}:system_url::" }
  secret_arns        = [local.db]
  task_policy_json   = data.aws_iam_policy_document.send_jobs.json
}

# --- EventBridge Scheduler: copilot-scheduler --once every 5 minutes ---------------------------

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-eventbridge-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

locals {
  scheduler_family_arn = "arn:aws:ecs:${var.region}:${local.account}:task-definition/${module.scheduler.family}"
}

data "aws_iam_policy_document" "scheduler_run" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = ["${local.scheduler_family_arn}:*", local.scheduler_family_arn]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [module.cluster.arn]
    }
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [module.scheduler.task_role_arn, module.scheduler.execution_role_arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "run-scheduler-task"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_run.json
}

resource "aws_scheduler_schedule" "tick" {
  name                         = "${local.prefix}-scheduler-tick"
  description                  = "copilot-scheduler --once: the sweep, usage reports, digests, standup packs"
  schedule_expression          = var.scheduler_rate
  schedule_expression_timezone = "UTC"
  # Before the first deploy, and while the instance is stopped, a run fails and is dropped.
  state = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = module.cluster.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = local.scheduler_family_arn # the latest revision
      launch_type         = "EC2"
      task_count          = 1
    }
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }
}

# --- Observability and CI ------------------------------------------------------------------------

locals {
  app_log_groups = [for m in [module.gateway, module.api, module.worker, module.scheduler, module.migrate] : m.log_group]
  all_log_groups = concat(local.app_log_groups, [module.proxy.log_group, module.postgres.log_group, module.valkey.log_group])
}

module "observability" {
  source             = "../../modules/observability"
  name               = local.prefix
  budget_monthly_usd = var.budget_monthly_usd
  budget_emails      = var.budget_emails
  xray_sampling_rate = var.xray_sampling_rate
  log_groups         = local.app_log_groups
}

module "ci" {
  source              = "../../modules/ci"
  name                = local.prefix
  github_repo         = var.github_repo
  state_bucket        = "copilot-tfstate-${local.account}"
  ecr_repository_arns = values(module.ecr.arns)
  cluster_arn         = module.cluster.arn
  task_families       = [module.migrate.family, module.scheduler.family]
  pass_role_arns = flatten([for m in [module.proxy, module.gateway, module.api, module.worker, module.migrate, module.scheduler] :
  [m.task_role_arn, m.execution_role_arn]])
  image_tag_parameter_arn = aws_ssm_parameter.image_tag.arn
  log_group_arns          = [for g in local.all_log_groups : "arn:aws:logs:${var.region}:${local.account}:log-group:${g}"]
}
