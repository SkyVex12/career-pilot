# App secrets in Secrets Manager (ARCHITECTURE.md, "Secrets").
#
# - openai and deepgram: created without a value. Put the keys in by hand
#   (infra/README.md, "First-time setup"); the services that need them do not start until then.
# - database: the Postgres superuser's password and one password per app role, as JSON, with
#   ready-made connection URLs for each service. The passwords are ephemeral and written with
#   a write-only argument, so they are never in the Terraform state or a plan. They are made
#   once; bump `database_secret_version` only together with the rotation steps in
#   infra/README.md (Postgres keeps the superuser password it was initialized with).

variable "name" {
  type = string
}

variable "database_secret_version" {
  type    = number
  default = 1
}

variable "db_host" {
  type    = string
  default = "127.0.0.1"
}

variable "db_name" {
  type    = string
  default = "copilot"
}

locals {
  # app_owner owns the tables and is used by migrations only, which run as the superuser
  # here; it keeps NOLOGIN. The gateway's role gets a password but no task uses it until
  # phase 2 retrieval.
  roles = ["postgres", "app_api", "app_gateway", "app_worker", "app_system"]
}

resource "aws_secretsmanager_secret" "openai" {
  name                    = "${var.name}/openai-api-key"
  description             = "OpenAI API key for the gateway and the workers (plain string)"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "deepgram" {
  name                    = "${var.name}/deepgram-api-key"
  description             = "Deepgram API key: streaming STT (gateway), pre-recorded (workers)"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "database" {
  name                    = "${var.name}/database"
  description             = "Postgres passwords per role and connection URLs per service (JSON)"
  recovery_window_in_days = 0
}

ephemeral "random_password" "db" {
  for_each = toset(local.roles)
  length   = 32
  special  = false # used inside URLs
}

locals {
  pw = { for r in local.roles : r => ephemeral.random_password.db[r].result }
  url = {
    for r in local.roles :
    r => "postgresql://${r}:${local.pw[r]}@${var.db_host}:5432/${var.db_name}"
  }
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id = aws_secretsmanager_secret.database.id
  secret_string_wo = jsonencode({
    postgres      = local.pw["postgres"]
    app_api       = local.pw["app_api"]
    app_gateway   = local.pw["app_gateway"]
    app_worker    = local.pw["app_worker"]
    app_system    = local.pw["app_system"]
    superuser_url = local.url["postgres"]
    api_url       = local.url["app_api"]
    gateway_url   = local.url["app_gateway"]
    worker_url    = local.url["app_worker"]
    system_url    = local.url["app_system"]
  })
  secret_string_wo_version = var.database_secret_version
}

output "openai_arn" {
  value = aws_secretsmanager_secret.openai.arn
}

output "deepgram_arn" {
  value = aws_secretsmanager_secret.deepgram.arn
}

output "database_arn" {
  value = aws_secretsmanager_secret.database.arn
}
