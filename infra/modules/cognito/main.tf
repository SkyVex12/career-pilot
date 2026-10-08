# Sign-in (ARCHITECTURE.md, "Desktop client": Login): a user pool with email as the username,
# invite-only (an admin creates each user; Cognito emails a temporary password), and one public
# app client for the desktop app: the authorization code flow with PKCE in the browser, back to
# a fixed loopback port. The pool's own domain (<prefix>.auth.<region>.amazoncognito.com) serves
# the sign-in pages over HTTPS; a custom domain can replace it later without app changes, since
# the app reads the domain from GET /v1/auth/config.
#
# With email as the username attribute, each user's Cognito username is their `sub`, which is
# the app's user id; user.delete deletes it by that id.

variable "name" {
  type = string
}

variable "domain_prefix" {
  type        = string
  description = "Globally unique within the region: <prefix>.auth.<region>.amazoncognito.com"
}

variable "callback_port" {
  type        = number
  default     = 53682
  description = "The desktop app's loopback port for the OAuth redirect (apps/desktop/core/src/auth.rs)"
}

variable "access_token_minutes" {
  type    = number
  default = 60
}

variable "refresh_token_days" {
  type    = number
  default = 30
}

data "aws_region" "current" {}

resource "aws_cognito_user_pool" "this" {
  name                     = var.name
  user_pool_tier           = "LITE" # the hosted sign-in pages, free up to 10,000 users a month
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "INACTIVE" # the stage can be torn down
  mfa_configuration        = "OFF"

  admin_create_user_config {
    allow_admin_create_user_only = true
    invite_message_template {
      email_subject = "Your Copilot account"
      email_message = "Sign in to Copilot with {username} and the temporary password {####}. You will choose your own password."
      sms_message   = "Copilot: {username} / {####}"
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 3
      max_length = 254
    }
  }

  lifecycle {
    ignore_changes = [schema] # Cognito cannot change schema attributes after creation
  }
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "desktop" {
  name         = "${var.name}-desktop"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret                      = false # a desktop app cannot keep one; PKCE instead
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls = [
    "http://127.0.0.1:${var.callback_port}/callback",
    "http://localhost:${var.callback_port}/callback",
  ]
  logout_urls = [
    "http://127.0.0.1:${var.callback_port}/signed-out",
  ]
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]

  access_token_validity  = var.access_token_minutes
  id_token_validity      = var.access_token_minutes
  refresh_token_validity = var.refresh_token_days
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"
}

output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.this.arn
}

output "client_id" {
  value = aws_cognito_user_pool_client.desktop.id
}

output "domain_url" {
  value = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${data.aws_region.current.region}.amazoncognito.com"
}
