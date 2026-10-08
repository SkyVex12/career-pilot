# One ECS workload on the Stage 0 instance: a task definition with one container, its log
# group, its own execution role (image pull, logs, and only its own secrets) and its own task
# role (what the code may do in AWS, from the role table in ARCHITECTURE.md), plus an ECS
# service when it runs all the time. One-off tasks (migrations, the scheduler tick) get the
# task definition only.
#
# Stage 0 runs every container with host networking on one instance, so two copies of a
# service cannot run at once (they would share a port): deploys stop the old task, then start
# the new one (minimum healthy 0 %, maximum 100 %), and the desktop client resumes its session
# meanwhile. The container definition itself is what a Fargate task would use in Stage 1; only
# the network mode, the launch type and the deployment percentages change.

variable "name" {
  type        = string
  description = "Workload name, e.g. gateway"
}

variable "prefix" {
  type = string
}

variable "cluster_arn" {
  type = string
}

variable "image" {
  type = string
}

variable "command" {
  type    = list(string)
  default = null
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "secrets" {
  type        = map(string)
  default     = {}
  description = "Environment variable name to Secrets Manager valueFrom (ARN, or ARN:json-key::)"
}

variable "secret_arns" {
  type        = list(string)
  default     = []
  description = "The secrets the execution role may read (ARNs without a JSON key)"
}

variable "task_policy_json" {
  type    = string
  default = null
}

variable "memory_reservation" {
  type        = number
  description = "MiB the scheduler sets aside for the container (soft limit)"
}

variable "memory" {
  type        = number
  description = "MiB at which the container is killed (hard limit)"
}

variable "user" {
  type    = string
  default = null
}

variable "health_check" {
  type    = list(string)
  default = null
}

variable "health_start_period" {
  type    = number
  default = 30
}

variable "host_mounts" {
  type        = map(string)
  default     = {}
  description = "Host path to container path"
}

variable "stop_timeout" {
  type    = number
  default = 30
}

variable "run_as_service" {
  type    = bool
  default = true
}

variable "desired_count" {
  type        = number
  default     = 1
  description = "0 until the service's image exists (before the first deploy)"
}

variable "log_retention_days" {
  type = number
}

variable "enable_exec" {
  type    = bool
  default = true
}

data "aws_region" "current" {}

locals {
  family = "${var.prefix}-${var.name}"
  mounts = { for i, host in keys(var.host_mounts) : "vol${i}" => { host = host, container = var.host_mounts[host] } }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/${var.prefix}/${var.name}"
  retention_in_days = var.log_retention_days
}

# --- Execution role: what ECS needs to start the container ----------------------------------

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.family}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "secrets" {
  count = length(var.secret_arns) > 0 ? 1 : 0
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = var.secret_arns
  }
}

resource "aws_iam_role_policy" "secrets" {
  count  = length(var.secret_arns) > 0 ? 1 : 0
  name   = "read-own-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.secrets[0].json
}

# --- Task role: what the code may do ------------------------------------------------------------

resource "aws_iam_role" "task" {
  name               = "${local.family}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "task" {
  count  = var.task_policy_json == null ? 0 : 1
  name   = "service"
  role   = aws_iam_role.task.id
  policy = var.task_policy_json
}

data "aws_iam_policy_document" "exec" {
  statement {
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "exec" {
  count  = var.enable_exec && var.run_as_service ? 1 : 0
  name   = "ecs-exec"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.exec.json
}

# --- Task definition -------------------------------------------------------------------------

resource "aws_ecs_task_definition" "this" {
  family                   = local.family
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  dynamic "volume" {
    for_each = local.mounts
    content {
      name      = volume.key
      host_path = volume.value.host
    }
  }

  container_definitions = jsonencode([merge(
    {
      name              = var.name
      image             = var.image
      essential         = true
      memoryReservation = var.memory_reservation
      memory            = var.memory
      stopTimeout       = var.stop_timeout
      environment       = [for k in sort(keys(var.environment)) : { name = k, value = var.environment[k] }]
      secrets           = [for k in sort(keys(var.secrets)) : { name = k, valueFrom = var.secrets[k] }]
      mountPoints       = [for k, m in local.mounts : { sourceVolume = k, containerPath = m.container, readOnly = false }]
      linuxParameters   = { initProcessEnabled = true }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.this.name
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = var.name
          mode                  = "non-blocking"
          max-buffer-size       = "4m"
        }
      }
    },
    var.command == null ? {} : { command = var.command },
    var.user == null ? {} : { user = var.user },
    var.health_check == null ? {} : {
      healthCheck = {
        command     = var.health_check
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = var.health_start_period
      }
    },
  )])
}

# --- Service -----------------------------------------------------------------------------------

resource "aws_ecs_service" "this" {
  count                              = var.run_as_service ? 1 : 0
  name                               = var.name
  cluster                            = var.cluster_arn
  task_definition                    = aws_ecs_task_definition.this.arn
  desired_count                      = var.desired_count
  launch_type                        = "EC2"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  enable_execute_command             = var.enable_exec
  propagate_tags                     = "SERVICE"
  wait_for_steady_state              = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

output "family" {
  value = aws_ecs_task_definition.this.family
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.this.name
}

output "service_name" {
  value = var.run_as_service ? aws_ecs_service.this[0].name : null
}
