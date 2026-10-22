output "external_ip" {
  description = "The VM's static IP: the desktop app's Cloud host"
  value       = local.host_ip
}

output "api_url" {
  description = "The desktop app's cloud API URL"
  value       = local.api_url
}

output "gateway_url" {
  description = "The desktop app's cloud gateway URL"
  value       = local.gateway_url
}

output "vm" {
  description = "What infra/gcp/scripts/stage.py needs"
  value = {
    project       = var.project_id
    instance      = module.host.instance
    zone          = module.host.zone
    http_firewall = module.host.http_firewall
    data_disk     = module.host.data_disk
  }
}

output "registry" {
  value = local.registry
}

output "buckets" {
  value = {
    uploads  = google_storage_bucket.uploads.name
    backups  = google_storage_bucket.backups.name
    releases = google_storage_bucket.releases.name
  }
}

output "service_accounts" {
  value = {
    vm      = module.host.service_account
    uploads = google_service_account.uploads.email
    ci      = module.ci.service_account
    deploy  = module.ci.deploy_service_account
    release = module.ci.release_service_account
  }
}

output "secrets" {
  description = "Created empty; README.md, Deploying Stage 0, fills them"
  value       = [for s in google_secret_manager_secret.app : s.secret_id]
}

output "kms_key" {
  value = local.kms_key
}

output "ci" {
  description = "Repository variables for .github/workflows/deploy.yml and release-desktop.yml"
  value = {
    GCP_WORKLOAD_IDENTITY_PROVIDER = module.ci.workload_identity_provider
    GCP_CI_SERVICE_ACCOUNT         = module.ci.service_account
    GCP_DEPLOY_SERVICE_ACCOUNT     = module.ci.deploy_service_account
    GCP_RELEASE_SERVICE_ACCOUNT    = module.ci.release_service_account
    GCP_REGISTRY                   = local.registry
    GCP_RELEASES_BUCKET            = google_storage_bucket.releases.name
  }
}

output "desktop" {
  description = "The release builds' settings: apps/desktop/release.json (stage.py desktop-config writes it)"
  value = {
    api_url      = local.api_url
    gateway_url  = local.gateway_url
    update_url   = "https://storage.googleapis.com/${google_storage_bucket.releases.name}/desktop/latest.json"
    download_url = "https://storage.googleapis.com/${google_storage_bucket.releases.name}/desktop/Copilot-setup.exe"
  }
}
