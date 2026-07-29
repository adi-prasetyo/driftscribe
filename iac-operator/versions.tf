# Mirrors iac/versions.tf — same OpenTofu floor, same provider major, same
# encrypted GCS backend — with one deliberate difference: `prefix`. A distinct
# prefix in the same bucket is what makes this a genuinely separate root, so
# neither this state nor the agent-facing one in `iac/` can read, lock, or
# overwrite the other.
terraform {
  required_version = ">= 1.12"

  required_providers {
    google = {
      source  = "hashicorp/google" # resolves via registry.opentofu.org
      version = "~> 6.0"
    }
  }

  backend "gcs" {
    bucket = "driftscribe-hack-2026-tofu-state" # MUST pre-exist (bootstrap)
    prefix = "operator"                         # NOT "prod" — see iac/versions.tf
  }

  encryption {
    key_provider "gcp_kms" "main" {
      kms_encryption_key = var.tofu_state_kms_key # full key resource path
      key_length         = 32                     # AES-256
    }
    method "aes_gcm" "primary" {
      keys = key_provider.gcp_kms.main
    }
    state {
      method   = method.aes_gcm.primary
      enforced = true
    }
    plan {
      method   = method.aes_gcm.primary
      enforced = true
    }
  }
}
