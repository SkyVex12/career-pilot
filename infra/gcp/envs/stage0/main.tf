# Stage 0 on GCP: every container on one Compute Engine VM (Container-Optimized OS), with
# Postgres, Valkey and ElasticMQ beside the app on a persistent data disk, and managed services
# only where they cost almost nothing: Cloud Storage, Cloud KMS, Secret Manager, Cloud Logging,
# Artifact Registry and a billing budget. The same shape as infra/envs/stage0 on AWS (which
# stays in the repository, unused). See README.md, "Deploying Stage 0", and infra/gcp/README.md.

locals {
  prefix   = var.name
  registry = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
  host_ip  = module.host.external_ip

  api_url     = "http://${local.host_ip}/v1"
  gateway_url = "ws://${local.host_ip}/v1/session"

  kms_key = google_kms_crypto_key.user_data.id

  # Secret Manager secrets, created empty: README.md ("Secrets") fills them, by hand for the
  # API keys and the OAuth client, by `stage.py secrets generate` for the rest.
  secrets = {
    openai-api-key             = "OpenAI API key: the gateway and the workers"
    deepgram-api-key           = "Deepgram API key: streaming STT (gateway), pre-recorded (workers)"
    db-postgres                = "Postgres superuser password (migrations, backups)"
    db-app-api                 = "Postgres password of app_api"
    db-app-gateway             = "Postgres password of app_gateway (no service uses it until phase 2)"
    db-app-worker              = "Postgres password of app_worker"
    db-app-system              = "Postgres password of app_system"
    uploads-hmac-access-id     = "HMAC access id of the uploads service account (S3 API to Cloud Storage)"
    uploads-hmac-secret        = "HMAC secret of the uploads service account"
    google-oauth-client-secret = "The desktop OAuth client's secret (Google sign-in)"
  }
  secret = { for k, _ in local.secrets : k => "{{secret:${local.prefix}-${k}}}" }

  db_url = { for role, s in {
    postgres   = "db-postgres"
    app_api    = "db-app-api"
    app_worker = "db-app-worker"
    app_system = "db-app-system"
  } : role => "postgresql://${role}:${local.secret[s]}@127.0.0.1:5432/copilot" }

  # The environment of each workload, as copilotctl reads it from the `copilot-env` metadata
  # value: [common] for every container, then one section per workload.
  common_env = {
    AWS_REGION                    = "us-east-1" # boto3 signs ElasticMQ requests for a region
    AWS_ACCESS_KEY_ID             = "local"     # ElasticMQ takes any credentials; these also keep
    AWS_SECRET_ACCESS_KEY         = "local"     # boto3 from probing the metadata server for AWS ones
    COPILOT_ENV                   = var.copilot_env
    COPILOT_LOG_FORMAT            = "json"
    COPILOT_VALKEY_URL            = "redis://127.0.0.1:6379/0"
    COPILOT_SQS_ENDPOINT          = "http://127.0.0.1:9324"
    COPILOT_SQS_QUEUE_URL         = "http://127.0.0.1:9324/000000000000/jobs"
    COPILOT_AUTH                  = "google"
    COPILOT_GOOGLE_CLIENT_IDS     = var.google_oauth_client_id
    COPILOT_GOOGLE_ALLOWED_EMAILS = join(",", var.allowed_emails)
  }
  model_env = merge(
    {
      COPILOT_LLM_PROVIDERS                      = "openai"
      COPILOT_EMBEDDER                           = "openai"
      COPILOT_OPENAI_EMBED_MODEL                 = var.openai_models["embed"]
      COPILOT_OPENAI_FIRST_TOKEN_TIMEOUT_MS      = tostring(var.openai_first_token_timeout_ms)
      COPILOT_OPENAI_DEEP_FIRST_TOKEN_TIMEOUT_MS = tostring(var.openai_deep_first_token_timeout_ms)
      COPILOT_MODEL_PRICES                       = jsonencode(var.model_prices)
      OPENAI_API_KEY                             = local.secret["openai-api-key"]
      DEEPGRAM_API_KEY                           = local.secret["deepgram-api-key"]
    },
    { for job, model in var.openai_models : "COPILOT_OPENAI_${upper(job)}_MODEL" => model if job != "embed" },
    { for job, effort in var.openai_reasoning : "COPILOT_OPENAI_${upper(job)}_REASONING" => effort },
  )
  # The API and the workers: Cloud Storage through its S3 API, and Cloud KMS.
  data_env = {
    COPILOT_S3_ENDPOINT   = "https://storage.googleapis.com"
    COPILOT_S3_REGION     = "auto"
    COPILOT_S3_BUCKET     = google_storage_bucket.uploads.name
    COPILOT_S3_ACCESS_KEY = local.secret["uploads-hmac-access-id"]
    COPILOT_S3_SECRET_KEY = local.secret["uploads-hmac-secret"]
    COPILOT_KEY_PROVIDER  = "gcp_kms"
    COPILOT_GCP_KMS_KEY   = local.kms_key
  }
  env = {
    common = local.common_env
    postgres = {
      POSTGRES_DB       = "copilot"
      POSTGRES_USER     = "postgres"
      POSTGRES_PASSWORD = local.secret["db-postgres"]
      PGDATA            = "/pgroot/pgdata"
    }
    gateway = merge(local.model_env, { HOST = "127.0.0.1", PORT = "8081" })
    api = merge(local.data_env, {
      HOST                         = "127.0.0.1"
      PORT                         = "8080"
      COPILOT_API_DATABASE_URL     = local.db_url["app_api"]
      COPILOT_GOOGLE_CLIENT_SECRET = local.secret["google-oauth-client-secret"]
    })
    worker = merge(local.model_env, local.data_env, {
      HOST                        = "127.0.0.1"
      PORT                        = "8082"
      COPILOT_SHUTDOWN_GRACE_S    = "100"
      COPILOT_WORKER_DATABASE_URL = local.db_url["app_worker"]
      COPILOT_SYSTEM_DATABASE_URL = local.db_url["app_system"]
    })
    # One-off before every deploy: migrations, then the app roles' logins.
    migrate = {
      COPILOT_DATABASE_URL            = local.db_url["postgres"]
      COPILOT_DB_PASSWORD_APP_API     = local.secret["db-app-api"]
      COPILOT_DB_PASSWORD_APP_GATEWAY = local.secret["db-app-gateway"]
      COPILOT_DB_PASSWORD_APP_WORKER  = local.secret["db-app-worker"]
      COPILOT_DB_PASSWORD_APP_SYSTEM  = local.secret["db-app-system"]
    }
    scheduler = { COPILOT_SYSTEM_DATABASE_URL = local.db_url["app_system"] }
  }
  copilot_env = join("\n", [for section, vars in local.env : join("\n", concat(
    ["[${section}]"], [for k in sort(keys(vars)) : "${k}=${vars[k]}"]
  ))])
}

# --- APIs ----------------------------------------------------------------------------------------
# Terraform needs Cloud Resource Manager and Service Usage on before it can turn on the rest;
# on a new project run once: gcloud services enable cloudresourcemanager.googleapis.com

resource "google_project_service" "apis" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "iap.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "oslogin.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Network -------------------------------------------------------------------------------------

resource "google_compute_network" "vpc" {
  name                    = local.prefix
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "vm" {
  name                     = "${local.prefix}-${var.region}"
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = "10.10.0.0/24"
  private_ip_google_access = true
}

# --- Images --------------------------------------------------------------------------------------

resource "google_artifact_registry_repository" "images" {
  repository_id = "copilot"
  location      = var.region
  format        = "DOCKER"
  description   = "gateway, api, worker and proxy images, tagged with the git SHA (linux/amd64 and linux/arm64)"

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.image_keep_count
    }
  }
  cleanup_policies {
    id     = "delete-the-rest"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "86400s"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "vm_pull" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${module.host.service_account}"
}

# --- Object storage: uploads (S3 API with HMAC keys) and backups -------------------------------

resource "google_storage_bucket" "uploads" {
  name                        = "${local.prefix}-uploads-${var.project_id}"
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # teardown deletes the uploads
  depends_on                  = [google_project_service.apis]
}

# The API and the workers reach the bucket through Cloud Storage's S3-compatible XML API (the
# code's S3 store, presigned uploads included), with an HMAC key of this account. The key is
# made by `stage.py secrets generate` straight into Secret Manager, so it is never in the
# Terraform state. The organization's iam.disableServiceAccountKeyCreation policy forbids
# making it, so that command lifts the policy for this project around the one call
# (infra/gcp/README.md, "The uploads HMAC key").
resource "google_service_account" "uploads" {
  account_id   = "${local.prefix}-uploads"
  display_name = "Copilot Stage 0 uploads (HMAC key for the S3 API)"
  depends_on   = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "uploads" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.uploads.email}"
}

resource "google_storage_bucket" "backups" {
  name                        = "${local.prefix}-backups-${var.project_id}"
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  lifecycle_rule {
    condition {
      age = var.backup_days
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "backups" {
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectUser" # write tonight's dump, read one back to restore
  member = "serviceAccount:${module.host.service_account}"
}

# --- Desktop releases: the installers and the update manifest, readable by anyone -------------

# The desktop app's installer and latest.json (the Tauri updater's manifest), published by
# .github/workflows/release-desktop.yml. Public: teammates download the installer with a
# plain link and the installed apps read the manifest without signing in. Nothing secret
# goes here, and the updater checks every download against the public key in the app.
resource "google_storage_bucket" "releases" {
  name                        = "${local.prefix}-releases-${var.project_id}"
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "inherited"
  force_destroy               = true
  versioning {
    enabled = true # a replaced latest.json or installer can be restored
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 3
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "releases_public" {
  bucket = google_storage_bucket.releases.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

# --- Per-user encryption: Cloud KMS ------------------------------------------------------------

# Key rings cannot be deleted in GCP: teardown removes the key's versions (after a day) and
# leaves the empty ring, which costs nothing (infra/gcp/README.md).
resource "google_kms_key_ring" "copilot" {
  name       = local.prefix
  location   = var.region
  depends_on = [google_project_service.apis]
}

# One symmetric key for user data keys. Each data key is wrapped with the user id as
# additional authenticated data, so it unwraps only for that user (copilot_core.crypto).
resource "google_kms_crypto_key" "user_data" {
  name                       = "user-data"
  key_ring                   = google_kms_key_ring.copilot.id
  purpose                    = "ENCRYPT_DECRYPT"
  rotation_period            = "31536000s" # yearly; old versions still decrypt
  destroy_scheduled_duration = "86400s"
}

resource "google_kms_crypto_key_iam_member" "vm" {
  crypto_key_id = google_kms_crypto_key.user_data.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${module.host.service_account}"
}

# --- Secrets -------------------------------------------------------------------------------------

resource "google_secret_manager_secret" "app" {
  for_each  = local.secrets
  secret_id = "${local.prefix}-${each.key}"
  annotations = {
    description = each.value
  }
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "vm" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${module.host.service_account}"
}

# --- The VM --------------------------------------------------------------------------------------

module "host" {
  source            = "../../modules/host"
  depends_on        = [google_project_service.apis]
  name              = local.prefix
  project_id        = var.project_id
  region            = var.region
  zone              = var.zone
  network           = google_compute_network.vpc.id
  subnetwork        = google_compute_subnetwork.vm.id
  machine_type      = var.machine_type
  boot_image        = var.boot_image
  boot_disk_gb      = var.boot_disk_gb
  data_disk_gb      = var.data_disk_gb
  allowed_cidrs     = var.open_to_internet ? ["0.0.0.0/0"] : var.allowed_cidrs
  scheduler_minutes = var.scheduler_minutes
  backup_time_utc   = var.backup_time_utc
  metadata = {
    copilot-env             = local.copilot_env
    copilot-registry        = local.registry
    copilot-image-postgres  = var.postgres_image
    copilot-image-valkey    = var.valkey_image
    copilot-image-elasticmq = var.elasticmq_image

    copilot-backup-bucket = google_storage_bucket.backups.name
  }
}

# --- Budget --------------------------------------------------------------------------------------

resource "google_monitoring_notification_channel" "budget" {
  for_each     = toset(var.budget_emails)
  display_name = "Copilot budget: ${each.value}"
  type         = "email"
  labels = {
    email_address = each.value
  }
  depends_on = [google_project_service.apis]
}

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "${local.prefix} monthly"
  budget_filter {
    projects = ["projects/${var.project_number}"]
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_monthly_usd)
    }
  }
  dynamic "threshold_rules" {
    for_each = [0.5, 0.8, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }
  all_updates_rule {
    monitoring_notification_channels = [for c in google_monitoring_notification_channel.budget : c.id]
    disable_default_iam_recipients   = false
  }
  depends_on = [google_project_service.apis]
}

# --- CI ------------------------------------------------------------------------------------------

module "ci" {
  source              = "../../modules/ci"
  depends_on          = [google_project_service.apis]
  name                = local.prefix
  project_id          = var.project_id
  github_repo         = var.github_repo
  repository_id       = google_artifact_registry_repository.images.repository_id
  repository_location = google_artifact_registry_repository.images.location
  instance            = module.host.instance
  zone                = module.host.zone
  vm_service_account  = module.host.service_account
  releases_bucket     = google_storage_bucket.releases.name
}
