variable "region" {
  description = "Alibaba Cloud region (Singapore = ap-southeast-1 international free tier)."
  type        = string
  default     = "ap-southeast-1"
}

variable "access_key" {
  description = "Alibaba Cloud AccessKey ID. Pass via TF_VAR_access_key or secrets.tfvars. NEVER commit."
  type        = string
  sensitive   = true
  default     = ""
}

variable "secret_key" {
  description = "Alibaba Cloud AccessKey secret. Pass via TF_VAR_secret_key or secrets.tfvars. NEVER commit."
  type        = string
  sensitive   = true
  default     = ""
}

variable "namespace" {
  description = "Project namespace used to prefix resource names. Keep short, lowercase, globally-unique-safe (feeds OSS bucket + ACR repo names)."
  type        = string
  default     = "waspada"
}

variable "environment" {
  description = "Environment suffix (e.g. prod, staging, dev)."
  type        = string
  default     = "prod"
}

variable "acr_registry_domain" {
  description = "ACR registry domain (push images here in the WA-018 deploy step). Personal Edition default = registry.<region>.aliyuncs.com. Use VPC domain if running FC from a VPC."
  type        = string
  default     = "registry.ap-southeast-1.aliyuncs.com"
}

variable "fc_image_tag" {
  description = "Tag of the ACR image to deploy (owner pushes this in the WA-018 deploy step)."
  type        = string
  default     = "latest"
}

# ---------------------------------------------------------------------------
# OpenTofu auto-loads *.auto.tfvars or any *.tfvars passed via -var-file.
# The convention here is: copy deploy/iac/secrets.tfvars.example -> secrets.tfvars,
# then run: tofu apply -var-file=secrets.tfvars
# OR export TF_VAR_access_key / TF_VAR_secret_key.
# secrets.tfvars is gitignored.
# ---------------------------------------------------------------------------
