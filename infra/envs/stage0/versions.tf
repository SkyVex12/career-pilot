terraform {
  # 1.11+: write-only arguments (the database passwords never reach the state).
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # State in the bucket made by infra/bootstrap, with S3-native locking. The bucket name has
  # the account id in it, so it comes at init time:
  #   terraform init -backend-config="bucket=copilot-tfstate-<account id>"
  backend "s3" {
    key          = "stage0/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { app = "copilot", stage = "stage0", managed_by = "terraform" }
  }
}
