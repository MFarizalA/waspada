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
  description = "ACR Personal Edition registry domain. Personal Edition uses instance-specific endpoint: crpi-<id>.<region>.personal.cr.aliyuncs.com"
  type        = string
  default     = "crpi-6cd1t4pmi9pottyq.ap-southeast-1.personal.cr.aliyuncs.com"
}

variable "fc_image_tag" {
  description = <<-EOT
    ACR image tag the FC function runs. PINNED to an immutable git-sha tag (not
    the mutable `:v2`) so terraform state == config == what's deployed — no
    drift, and no surprise retag on `tofu apply`. build-image.yml pushes
    `:latest`, `:v2`, and `:<git-sha>`; deploy by bumping this to the new sha and
    applying. Currently: the HTMLResponse render-fix build (0ae6be8).
  EOT
  type        = string
  default     = "0ae6be8a6a7ffcd029cfab4185115dbf1332832d"
}

variable "rds_instance_type" {
  description = "RDS MySQL instance type for ap-southeast-1 (Singapore)."
  type        = string
  # Queried live API (alicloud_db_instance_classes) for ap-southeast-1b.
  # mysql.n2.medium.1 (Basic, 2 vCPU, 2 GB) — smallest type.
  default = "mysql.n2.medium.1"
}

variable "rds_password" {
  description = "RDS MySQL master password. Pass via TF_VAR_rds_password or secrets.tfvars. NEVER commit."
  type        = string
  sensitive   = true
  default     = ""
}

variable "duckdb_rds_endpoint" {
  description = "DuckDB RDS analytical instance endpoint (created via console, WA-060). Pass via TF_VAR_duckdb_rds_endpoint or secrets.tfvars."
  type        = string
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

variable "oss_endpoint_internal" {
  description = "Internal VPC endpoint for OSS (e.g. oss-ap-southeast-1-internal.aliyuncs.com). Optional; defaults to the public endpoint."
  type        = string
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
