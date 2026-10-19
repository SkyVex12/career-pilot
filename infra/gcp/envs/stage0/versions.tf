terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.7"
    }
  }

  # State in the bucket made by infra/gcp/bootstrap (GCS locks it natively). The bucket name
  # has the project id in it, so it comes at init time:
  #   terraform init -backend-config="bucket=copilot-tfstate-<project id>"
  backend "gcs" {
    prefix = "stage0"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
  # Quota and billing for API calls go to this project, which the Billing Budgets API needs
  # when Terraform runs with a person's application-default credentials.
  user_project_override = true
  billing_project       = var.project_id
  default_labels = {
    app        = "copilot"
    stage      = "stage0"
    managed_by = "terraform"
  }
}
