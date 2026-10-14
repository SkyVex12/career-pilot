# Cost and logging guard rails (ARCHITECTURE.md, "Cost controls" and "Observability"):
# - an AWS Budgets alert per account, on the forecast and on actual spend;
# - the X-Ray sampling rule for the per-question spans (about 5 %);
# - a saved CloudWatch Logs Insights query that pulls one session's lines from every service.
# Log groups belong to the workloads (infra/modules/service), with the stage's retention.

variable "name" {
  type = string
}

variable "budget_monthly_usd" {
  type = number
}

variable "budget_emails" {
  type = list(string)
}

variable "forecast_alert_percents" {
  type    = list(number)
  default = [50, 80]
}

variable "xray_sampling_rate" {
  type = number
}

variable "log_groups" {
  type = list(string)
}

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_monthly_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = toset(var.forecast_alert_percents)
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = var.budget_emails
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_emails
  }
}

resource "aws_xray_sampling_rule" "questions" {
  rule_name      = substr("${var.name}-questions", 0, 32)
  priority       = 1000
  version        = 1
  reservoir_size = 1
  fixed_rate     = var.xray_sampling_rate
  url_path       = "*"
  host           = "*"
  http_method    = "*"
  service_type   = "*"
  service_name   = "*"
  resource_arn   = "*"
}

resource "aws_cloudwatch_query_definition" "session" {
  name            = "${var.name}/one session"
  log_group_names = var.log_groups
  query_string    = <<-EOT
    # Replace the id. Every gateway, API, worker and scheduler line about a session has it.
    fields @timestamp, service, level, logger, msg, job, @logStream
    | filter session_id = "00000000-0000-0000-0000-000000000000"
    | sort @timestamp asc
    | limit 10000
  EOT
}

resource "aws_cloudwatch_query_definition" "errors" {
  name            = "${var.name}/errors"
  log_group_names = var.log_groups
  query_string    = <<-EOT
    fields @timestamp, service, level, logger, msg, session_id, job, exc
    | filter level in ["ERROR", "CRITICAL"] or ispresent(exc)
    | sort @timestamp desc
    | limit 200
  EOT
}
