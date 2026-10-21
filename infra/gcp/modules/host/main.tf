# The Stage 0 VM: one Container-Optimized OS instance running every container (gateway, API,
# worker, nginx proxy, Postgres with pgvector, Valkey, ElasticMQ) under systemd, with the data
# on its own persistent disk, a static external IP, port 80 open only to the allow-list and
# SSH only through IAP. See infra/gcp/README.md.

variable "name" {
  type = string
}

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "zone" {
  type = string
}

variable "network" {
  type = string
}

variable "subnetwork" {
  type = string
}

variable "machine_type" {
  type = string
}

variable "boot_image" {
  type        = string
  description = "A Container-Optimized OS image or family, e.g. projects/cos-cloud/global/images/family/cos-stable"
}

variable "boot_disk_gb" {
  type = number
}

variable "data_disk_gb" {
  type = number
}

variable "allowed_cidrs" {
  type = list(string)
}

variable "metadata" {
  type        = map(string)
  description = "Instance metadata copilotctl reads: copilot-env, copilot-registry, copilot-image-*, copilot-backup-bucket"
}

variable "scheduler_minutes" {
  type = number
}

variable "backup_time_utc" {
  type        = string
  description = "HH:MM:SS, UTC"
}

locals {
  tag = "${var.name}-vm"
  # Workloads run as systemd units, with the seconds `docker stop` waits for each.
  services = {
    postgres  = 90
    valkey    = 30
    elasticmq = 10
    proxy     = 10
    gateway   = 30
    api       = 15
    worker    = 110 # lets running jobs finish (shutdown_grace_s 100)
  }
}

# --- Identity ------------------------------------------------------------------------------------

resource "google_service_account" "vm" {
  account_id   = "${var.name}-vm"
  display_name = "Copilot Stage 0 VM"
  description  = "The VM's containers: Cloud KMS (API and workers), Secret Manager and Artifact Registry reads (the host), logs, backups"
}

resource "google_project_iam_member" "logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# --- Network edge --------------------------------------------------------------------------------

resource "google_compute_address" "ip" {
  name         = "${var.name}-ip"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
}

# Port 80 from the allow-list only (Stage 0 has no TLS), or from anywhere when the stage sets
# open_to_internet. `infra/gcp/scripts/stage.py allow` changes the list in one command, so
# Terraform sets it at creation and then leaves it alone.
resource "google_compute_firewall" "http" {
  name = "${var.name}-http-allowlist"
  lifecycle {
    ignore_changes = [source_ranges]
    precondition {
      condition     = length(var.allowed_cidrs) > 0
      error_message = "allowed_cidrs is empty: list the team's addresses, or set open_to_internet = true."
    }
  }
  network       = var.network
  description   = "Plain HTTP and WebSocket to nginx on the Stage 0 VM, from the team's addresses only"
  direction     = "INGRESS"
  source_ranges = var.allowed_cidrs
  target_tags   = [local.tag]
  allow {
    protocol = "tcp"
    ports    = ["80"]
  }
}

# SSH only through Identity-Aware Proxy (its fixed range), with OS Login: no open port 22.
resource "google_compute_firewall" "iap_ssh" {
  name          = "${var.name}-iap-ssh"
  network       = var.network
  description   = "SSH through IAP TCP forwarding only"
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.tag]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# --- Disks and the instance ----------------------------------------------------------------------

# Postgres, Valkey, ElasticMQ, the deployed image tag and the newest dump. A separate disk, so
# it survives stops, deploys and replacing the VM. Teardown deletes it (the nightly dumps are
# in the backups bucket until then).
resource "google_compute_disk" "data" {
  name = "${var.name}-data"
  zone = var.zone
  type = "pd-balanced"
  size = var.data_disk_gb
}

resource "google_compute_instance" "vm" {
  name                      = var.name
  zone                      = var.zone
  machine_type              = var.machine_type
  tags                      = [local.tag]
  allow_stopping_for_update = true

  boot_disk {
    auto_delete = true
    initialize_params {
      image = var.boot_image
      size  = var.boot_disk_gb
      type  = "pd-balanced"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "copilot-data" # /dev/disk/by-id/google-copilot-data
  }

  network_interface {
    subnetwork = var.subnetwork
    access_config {
      nat_ip       = google_compute_address.ip.address
      network_tier = "PREMIUM"
    }
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"] # what it may do is decided by IAM, not scopes
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = merge(var.metadata, {
    enable-oslogin               = "TRUE"
    block-project-ssh-keys       = "TRUE"
    google-logging-enabled       = "true"
    google-logging-use-fluentbit = "true"
    google-monitoring-enabled    = "false"
    user-data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
      copilotctl        = filebase64("${path.module}/../../host/copilotctl.sh")
      elasticmq_conf    = filebase64("${path.module}/../../host/elasticmq.conf")
      services          = local.services
      scheduler_minutes = var.scheduler_minutes
      backup_time_utc   = var.backup_time_utc
    })
  })

  lifecycle {
    # A newer COS image in the family never replaces the VM by surprise; see infra/gcp/README.md.
    ignore_changes = [boot_disk[0].initialize_params[0].image]
  }

  depends_on = [google_project_iam_member.logs]
}

output "service_account" {
  value = google_service_account.vm.email
}

output "external_ip" {
  value = google_compute_address.ip.address
}

output "instance" {
  value = google_compute_instance.vm.name
}

output "zone" {
  value = var.zone
}

output "http_firewall" {
  value = google_compute_firewall.http.name
}

output "data_disk" {
  value = google_compute_disk.data.name
}
