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

variable "rds_instance_type" {
  description = "RDS PostgreSQL instance type. pg.n2.small.1 is the cheapest (free-tier eligible)."
  type        = string
  default     = "pg.n2.small.1"
}

variable "rds_password" {
  description = "RDS PostgreSQL master password. Pass via TF_VAR_rds_password or secrets.tfvars. NEVER commit."
  type        = string
  sensitive   = true
  default     = ""
}

variable "rds_security_ips" {
  description = "CIDR blocks allowed to reach the RDS instance. Defaults to the FC VPC subnet range. Add a maintenance IP (e.g. your office CIDR) via TF_VAR_rds_security_ips or secrets.tfvars. NEVER use 0.0.0.0/0 in production."
  type        = list(string)
  default     = ["172.16.0.0/12"]
}

variable "dashscope_api_key" {
  description = "Qwen Cloud / DashScope API key. Pass via TF_VAR_dashscope_api_key or secrets.tfvars. NEVER commit."
  type        = string
  sensitive   = true
  default     = ""
}

variable "waspada_jwt_secret" {
  description = "JWT signing secret (min 32 bytes). Generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\". NEVER commit."
  type        = string
  sensitive   = true
  default     = ""
}

# ---------------------------------------------------------------------------
# OpenTofu auto-loads *.auto.tfvars or any *.tfvars passed via -var-file.
# The convention here is: copy deploy/iac/secrets.tfvars.example -> secrets.tfvars,
# then run: tofu apply -var-file=secrets.tfvars
# OR export TF_VAR_access_key / TF_VAR_secret_key.
# secrets.tfvars is gitignored.
# ---------------------------------------------------------------------------
