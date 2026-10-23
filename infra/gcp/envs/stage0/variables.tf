variable "name" {
  type        = string
  description = "Prefix for every resource name"
}

variable "project_id" {
  type = string
}

variable "project_number" {
  type        = string
  description = "For the budget's project filter (gcloud projects describe <id> --format='value(projectNumber)')"
}

variable "billing_account" {
  type        = string
  description = "The billing account the budget watches, e.g. 01E781-540C4F-0EF7E4"
}

variable "region" {
  type = string
}

variable "zone" {
  type = string
}

variable "copilot_env" {
  type        = string
  description = "COPILOT_ENV for the services: staging or prod (dev and test refuse to run on Compute Engine)"
  validation {
    condition     = contains(["staging", "prod"], var.copilot_env)
    error_message = "Cloud deployments use staging or prod."
  }
}

variable "min_desktop_version" {
  type        = string
  default     = "0.1.0"
  description = "The oldest desktop app the gateway and the API serve (COPILOT_MIN_CLIENT_VERSION); older apps are told to update at once. Raise it only once the newer version is published (README.md, \"Releasing the desktop app\")."
  validation {
    condition     = can(regex("^[0-9]+[.][0-9]+[.][0-9]+$", var.min_desktop_version))
    error_message = "min_desktop_version is a version such as 0.2.0."
  }
}

# --- The VM --------------------------------------------------------------------------------------

variable "machine_type" {
  type = string
}

variable "boot_image" {
  type = string
}

variable "boot_disk_gb" {
  type = number
}

variable "data_disk_gb" {
  type = number
}

variable "allowed_cidrs" {
  type        = list(string)
  default     = []
  description = "Addresses allowed to reach port 80, at creation (later: stage.py allow). Kept out of git, in allowlist.auto.tfvars"
  validation {
    condition     = alltrue([for c in var.allowed_cidrs : can(cidrhost(c, 0)) && c != "0.0.0.0/0"])
    error_message = "Each entry must be a CIDR such as 203.0.113.7/32. To open port 80 to everyone, set open_to_internet = true instead."
  }
}

variable "open_to_internet" {
  type        = bool
  default     = false
  description = "Port 80 open to every address (0.0.0.0/0) instead of allowed_cidrs. Stage 0 has no TLS, so tokens and call text then cross any network in the clear; only the Google sign-in allow-list keeps strangers out"
}

# --- Images --------------------------------------------------------------------------------------

variable "postgres_image" {
  type = string
}

variable "valkey_image" {
  type = string
}

variable "elasticmq_image" {
  type = string
}

# --- Sign-in (Google, allow-listed) --------------------------------------------------------------

variable "google_oauth_client_id" {
  type        = string
  description = "The Desktop app OAuth client made in the Console (README.md, Deploying Stage 0)"
}

variable "allowed_emails" {
  type        = list(string)
  description = "Google accounts that may sign in. Kept out of git, in allowlist.auto.tfvars"
  validation {
    condition     = length(var.allowed_emails) > 0 && alltrue([for e in var.allowed_emails : can(regex("^[^@\\s]+@[^@\\s]+$", e))])
    error_message = "List at least one email."
  }
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

variable "scheduler_minutes" {
  type        = number
  description = "copilot-scheduler --once runs every this many minutes (a systemd timer on the VM)"
}

variable "backup_time_utc" {
  type = string
}

variable "backup_days" {
  type        = number
  description = "How long the backups bucket keeps each nightly dump"
}

variable "image_keep_count" {
  type        = number
  description = "Images kept per repository (older ones are deleted; rollbacks go back that far)"
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

variable "career_os_url" {
  description = "Career-OS's API base URL (https://...), for linking accounts and pulling applications; empty turns the link off"
  type        = string
  default     = ""
  validation {
    condition     = var.career_os_url == "" || startswith(var.career_os_url, "https://")
    error_message = "career_os_url must be an https:// URL (it carries the link token)."
  }
}
