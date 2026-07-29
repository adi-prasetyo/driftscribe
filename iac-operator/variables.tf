variable "project_id" {
  type    = string
  default = "driftscribe-hack-2026"
}
variable "region" {
  type    = string
  default = "asia-northeast1"
}
variable "tofu_state_kms_key" {
  type        = string
  description = "Full Cloud KMS key resource path for OpenTofu state/plan encryption (early-eval: var only)."
}
