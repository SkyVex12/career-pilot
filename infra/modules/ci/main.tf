# GitHub Actions roles, assumed through GitHub's OIDC provider: no long-lived AWS keys
# (ARCHITECTURE.md, "CI/CD").
#
# - plan: pull requests in the repository. Read-only access to the account plus the state
#   bucket (S3-native locking writes a lock object), for `terraform plan`. It cannot read
#   secret values: the database passwords are write-only and never in the state.
# - deploy: pushes to main only. Pushes images, registers task definitions, updates the ECS
#   services, runs the migration task and records the deployed image tag. It does not change
#   infrastructure; `terraform apply` stays a person's step in Stage 0.

variable "name" {
  type = string
}

variable "github_repo" {
  type        = string
  description = "owner/name"
}

variable "state_bucket" {
  type = string
}

variable "ecr_repository_arns" {
  type = list(string)
}

variable "cluster_arn" {
  type = string
}

variable "task_families" {
  type = list(string)
}

variable "pass_role_arns" {
  type = list(string)
}

variable "image_tag_parameter_arn" {
  type = string
}

variable "log_group_arns" {
  type = list(string)
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "trust_plan" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:pull_request",
        "repo:${var.github_repo}:ref:refs/heads/main",
      ]
    }
  }
}

data "aws_iam_policy_document" "trust_deploy" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

# --- plan ------------------------------------------------------------------------------------

resource "aws_iam_role" "plan" {
  name                 = "${var.name}-ci-plan"
  assume_role_policy   = data.aws_iam_policy_document.trust_plan.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "plan_state" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.state_bucket}"]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.state_bucket}/*"]
  }
  # ReadOnlyAccess reads secret metadata but not values; the plan needs neither more.
  statement {
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "plan_state" {
  name   = "terraform-state"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan_state.json
}

# --- deploy ----------------------------------------------------------------------------------

resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-ci-deploy"
  assume_role_policy   = data.aws_iam_policy_document.trust_deploy.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = var.ecr_repository_arns
  }
  statement {
    sid = "TaskDefinitions"
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:ListTaskDefinitions",
    ]
    resources = ["*"] # these actions do not support resource-level permissions
  }
  statement {
    sid       = "Services"
    actions   = ["ecs:UpdateService", "ecs:DescribeServices"]
    resources = ["arn:aws:ecs:${local.region}:${local.account}:service/${element(split("/", var.cluster_arn), 1)}/*"]
  }
  statement {
    sid       = "MigrationTask"
    actions   = ["ecs:RunTask"]
    resources = [for f in var.task_families : "arn:aws:ecs:${local.region}:${local.account}:task-definition/${f}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [var.cluster_arn]
    }
  }
  statement {
    sid       = "Tasks"
    actions   = ["ecs:DescribeTasks", "ecs:ListTasks", "ecs:DescribeContainerInstances", "ecs:ListContainerInstances"]
    resources = ["*"]
  }
  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = var.pass_role_arns
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid       = "ImageTag"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [var.image_tag_parameter_arn]
  }
  statement {
    sid       = "MigrationLogs"
    actions   = ["logs:GetLogEvents", "logs:FilterLogEvents"]
    resources = [for a in var.log_group_arns : "${a}:*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

output "plan_role_arn" {
  value = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}
