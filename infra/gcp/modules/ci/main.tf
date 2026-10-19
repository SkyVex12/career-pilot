# CI on GCP (ARCHITECTURE.md, "CI/CD"): GitHub Actions signs in through Workload Identity
# Federation with GitHub's OIDC token, so no service account key exists anywhere. Only
# workflow runs on `main` of this repository get in, as a service account that can push images
# to the one Artifact Registry repository and nothing else. Deploying stays a person's command.

variable "name" {
  type = string
}

variable "project_id" {
  type = string
}

variable "github_repo" {
  type        = string
  description = "owner/name"
}

variable "repository_id" {
  type        = string
  description = "The Artifact Registry repository CI pushes to"
}

variable "repository_location" {
  type = string
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${var.name}-github"
  display_name              = "GitHub Actions"
  description               = "Workflow runs of ${var.github_repo}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Tokens from any other repository, or from a branch other than main, are refused here.
  attribute_condition = "assertion.repository == '${var.github_repo}' && assertion.ref == 'refs/heads/main'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "ci" {
  account_id   = "${var.name}-ci"
  display_name = "Copilot Stage 0 CI (image pushes)"
}

resource "google_service_account_iam_member" "github" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

resource "google_artifact_registry_repository_iam_member" "push" {
  location   = var.repository_location
  repository = var.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci.email}"
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "service_account" {
  value = google_service_account.ci.email
}
