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
    prefix = "prod"
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
