# The Terraform state bucket for Stage 0 on GCP, once per project (local state; it is the one
# thing infra/gcp/envs/stage0 cannot make for itself). Versioned, so an earlier state can be
# recovered; GCS locks the state natively.
#   terraform -chdir=infra/gcp/bootstrap init
#   terraform -chdir=infra/gcp/bootstrap apply

terraform {
  required_version = ">= 1.11"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.7"
    }
  }
}

variable "project_id" {
  type    = string
  default = "career-pilot-512115"
}

variable "region" {
  type    = string
  default = "us-central1"
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

resource "google_storage_bucket" "tfstate" {
  name                        = "copilot-tfstate-${var.project_id}"
  location                    = upper(var.region)
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 20
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
  labels = { app = "copilot", stage = "stage0", managed_by = "terraform" }
}

output "bucket" {
  value = google_storage_bucket.tfstate.name
}
