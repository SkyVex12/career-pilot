variable "name" {
  type        = string
  description = "Prefix for every resource name"
}

variable "region" {
  type = string
}

variable "copilot_env" {
  type        = string
  description = "COPILOT_ENV for the services: staging or prod (dev and test refuse to run on AWS)"
  validation {
    condition     = contains(["staging", "prod"], var.copilot_env)
    error_message = "AWS deployments use staging or prod."
  }
}

# --- Network and instance ----------------------------------------------------------------------

variable "az_count" {
  type = number
}

variable "instance_type" {
  type = string
}

variable "data_volume_gb" {
  type = number
}

variable "snapshot_days" {
  type = number
}

variable "allowed_cidrs" {
  type        = list(string)
  description = "Addresses allowed to reach port 80. Kept out of git: allowlist.auto.tfvars locally, the TF_ALLOWED_CIDRS repository variable in CI"
  validation {
    condition     = alltrue([for c in var.allowed_cidrs : can(cidrhost(c, 0)) && c != "0.0.0.0/0"])
    error_message = "Each entry must be a CIDR such as 203.0.113.7/32; 0.0.0.0/0 is refused (the app has no TLS in Stage 0)."
  }
}

# --- Images --------------------------------------------------------------------------------------

variable "postgres_image" {
  type = string
}

variable "valkey_image" {
  type = string
}

# --- Sign-in -------------------------------------------------------------------------------------

variable "cognito_domain_prefix" {
  type = string
}

# --- Models (OpenAI only in Stage 0) -------------------------------------------------------------

variable "openai_models" {
  type        = map(string)
  description = "Model per job: detector, fast, deep, extract, job, pack, postcall, embed"
}

variable "openai_reasoning" {
  type        = map(string)
  description = "Reasoning effort per job (empty: the model's default)"
}

variable "openai_first_token_timeout_ms" {
  type = number
}

variable "openai_deep_first_token_timeout_ms" {
  type = number
}

variable "model_prices" {
  type        = map(object({ input = number, cached = number, output = number }))
  description = "Per million tokens, for answers.cost_usd"
}

# --- Operations ----------------------------------------------------------------------------------

variable "log_retention_days" {
  type = number
}

variable "xray_sampling_rate" {
  type = number
}

variable "budget_monthly_usd" {
  type = number
}

variable "budget_emails" {
  type = list(string)
}

variable "github_repo" {
  type = string
}

variable "scheduler_rate" {
  type        = string
  description = "How often EventBridge Scheduler runs copilot-scheduler --once"
}
