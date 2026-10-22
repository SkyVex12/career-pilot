# CI/CD on GCP (ARCHITECTURE.md, "CI/CD"): GitHub Actions signs in through Workload Identity
# Federation with GitHub's OIDC token, so no service account key exists anywhere. Only runs of
# this repository get in, and only from `main` or a `desktop-v*` tag; each workflow then acts as
# one service account with the least it needs:
#
#   ci       (main)  push images to the one Artifact Registry repository
#   deploy   (main)  read that repository; start the VM; SSH to it through IAP with OS Login
#                    and sudo (copilotctl runs as root); nothing else in the project
#   release  (tags)  write the desktop installers and the update manifest to the releases bucket

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

variable "instance" {
  type        = string
  description = "The Stage 0 VM the deploy account may start and SSH to"
}

variable "zone" {
  type = string
}

variable "vm_service_account" {
  type        = string
  description = "The VM's service account (OS Login to a VM needs actAs on the account it runs as)"
}

variable "releases_bucket" {
  type = string
}

locals {
  pool = google_iam_workload_identity_pool.github.name
  # Who may act as each account: main's runs, or the desktop tags' runs.
  main_runs = "principalSet://iam.googleapis.com/${local.pool}/attribute.ref/refs/heads/main"
  tag_runs  = "principalSet://iam.googleapis.com/${local.pool}/attribute.ref_type/tag"
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
    "attribute.ref_type"   = "assertion.ref_type"
  }
  # Tokens from any other repository, or from any ref but main and the desktop release tags
  # (pull requests, other branches, other tags), are refused here.
  attribute_condition = "assertion.repository == '${var.github_repo}' && (assertion.ref == 'refs/heads/main' || assertion.ref.startsWith('refs/tags/desktop-v'))"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# --- ci: image pushes ----------------------------------------------------------------------------

resource "google_service_account" "ci" {
  account_id   = "${var.name}-ci"
  display_name = "Copilot Stage 0 CI (image pushes)"
}

resource "google_service_account_iam_member" "github" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.main_runs
}

resource "google_artifact_registry_repository_iam_member" "push" {
  location   = var.repository_location
  repository = var.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.ci.email}"
}

# --- deploy: the VM ------------------------------------------------------------------------------

resource "google_service_account" "deploy" {
  account_id   = "${var.name}-deploy"
  display_name = "Copilot Stage 0 deploy (CD to the VM)"
  description  = "deploy.yml on main: start the VM, SSH through IAP with OS Login, run copilotctl deploy"
}

resource "google_service_account_iam_member" "deploy_github" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.main_runs
}

# Check that the images of the commit are in the registry before touching the VM.
resource "google_artifact_registry_repository_iam_member" "deploy_read" {
  location   = var.repository_location
  repository = var.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.deploy.email}"
}

# On the VM only: read it and start it when it is stopped. Granted on the instance, so no
# other instance (there is none today) could be started.
resource "google_project_iam_custom_role" "deploy_instance" {
  role_id     = replace("${var.name}_deploy_instance", "-", "_")
  title       = "Copilot Stage 0 deploy: the VM"
  description = "Read the Stage 0 VM and start or resume it"
  permissions = [
    "compute.instances.get",
    "compute.instances.start",
    "compute.instances.resume",
  ]
}

resource "google_compute_instance_iam_member" "deploy_instance" {
  instance_name = var.instance
  zone          = var.zone
  role          = google_project_iam_custom_role.deploy_instance.id
  member        = "serviceAccount:${google_service_account.deploy.email}"
}

# gcloud reads the project (its OS Login metadata) and waits on the start operation: two
# read-only project permissions, which instance-level IAM cannot grant.
resource "google_project_iam_custom_role" "deploy_project" {
  role_id     = replace("${var.name}_deploy_project", "-", "_")
  title       = "Copilot Stage 0 deploy: project reads"
  description = "Read the project's metadata and zone operations (gcloud compute ssh, instances start)"
  permissions = [
    "compute.projects.get",
    "compute.zoneOperations.get",
  ]
}

resource "google_project_iam_member" "deploy_project" {
  project = var.project_id
  role    = google_project_iam_custom_role.deploy_project.id
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# SSH: OS Login with sudo (copilotctl runs as root), on this instance only, through IAP's
# TCP forwarding on this instance only. No SSH key is stored: gcloud makes one per run and
# OS Login keeps it for the account (with a TTL, set by the workflow).
resource "google_compute_instance_iam_member" "deploy_os_admin_login" {
  instance_name = var.instance
  zone          = var.zone
  role          = "roles/compute.osAdminLogin"
  member        = "serviceAccount:${google_service_account.deploy.email}"
}

resource "google_iap_tunnel_instance_iam_member" "deploy" {
  instance = var.instance
  zone     = var.zone
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "serviceAccount:${google_service_account.deploy.email}"
}

# OS Login to a VM that runs as a service account needs actAs on that account.
resource "google_service_account_iam_member" "deploy_act_as_vm" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.vm_service_account}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

# --- release: the desktop installers -------------------------------------------------------------

resource "google_service_account" "release" {
  account_id   = "${var.name}-release"
  display_name = "Copilot desktop releases"
  description  = "release-desktop.yml on desktop-v* tags: write installers and latest.json"
}

resource "google_service_account_iam_member" "release_github" {
  service_account_id = google_service_account.release.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.tag_runs
}

# Create and replace objects (latest.json and Copilot-setup.exe are replaced each release).
resource "google_storage_bucket_iam_member" "release_write" {
  bucket = var.releases_bucket
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.release.email}"
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "service_account" {
  value = google_service_account.ci.email
}

output "deploy_service_account" {
  value = google_service_account.deploy.email
}

output "release_service_account" {
  value = google_service_account.release.email
}
