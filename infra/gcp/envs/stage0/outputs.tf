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
    uploads = google_storage_bucket.uploads.name
    backups = google_storage_bucket.backups.name
  }
}

output "service_accounts" {
  value = {
    vm      = module.host.service_account
    uploads = google_service_account.uploads.email
    ci      = module.ci.service_account
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
  description = "Repository variables for .github/workflows/deploy.yml"
  value = {
    GCP_WORKLOAD_IDENTITY_PROVIDER = module.ci.workload_identity_provider
    GCP_CI_SERVICE_ACCOUNT         = module.ci.service_account
    GCP_REGISTRY                   = local.registry
  }
}
