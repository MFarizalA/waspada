# ---------------------------------------------------------------------------
# Local values
# ---------------------------------------------------------------------------
locals {
  name_prefix     = "${var.namespace}-${var.environment}"
  acr_namespace   = replace(local.name_prefix, "-", "")
  # Personal Edition ACR default internet domain. Override via var.acr_registry_domain
  # if using Enterprise Edition or a different endpoint.
  fc_image        = "${var.acr_registry_domain}/${local.acr_namespace}/api:${var.fc_image_tag}"
}

# ---------------------------------------------------------------------------
# Provider — Singapore region, AccessKey via TF_VAR_* env or secrets.tfvars
# ---------------------------------------------------------------------------
provider "alicloud" {
  region     = var.region
  access_key = var.access_key
  secret_key = var.secret_key
}

# ---------------------------------------------------------------------------
# OSS bucket — loan-portfolio Parquet data store (WA-018/WA-023)
# ACL is split out (inline `acl` deprecated since provider 1.220).
# ---------------------------------------------------------------------------
resource "alicloud_oss_bucket" "loans" {
  bucket = "${local.name_prefix}-loans"

  tags = {
    Project     = var.namespace
    Environment = var.environment
    ManagedBy   = "opentofu"
    Component   = "loans-oss"
  }
}

resource "alicloud_oss_bucket_acl" "loans" {
  bucket = alicloud_oss_bucket.loans.bucket
  acl    = "private"
}

# ---------------------------------------------------------------------------
# ACR Container Registry (Personal Edition, free tier) — hosts the FC image.
# Personal Edition uses namespace + repo directly (no instance resource).
# NOTE: alicloud_cr_namespace/repo carry deprecation warnings in favor of the
# Enterprise Edition (alicloud_cr_ee_*) resources; Personal remains free and
# functional for the hackathon. Switch to EE if you need VPC-only endpoints.
# ---------------------------------------------------------------------------
resource "alicloud_cr_namespace" "waspada" {
  name               = local.acr_namespace
  auto_create        = false
  default_visibility = "PRIVATE"
}

resource "alicloud_cr_repo" "api" {
  namespace = alicloud_cr_namespace.waspada.name
  name      = "api"
  summary   = "WASPADA FastAPI image for Function Compute (CAPort 8080)"
  repo_type = "PRIVATE"
  detail    = "Custom-container image consumed by the FC function."
}

# ---------------------------------------------------------------------------
# RAM role for Function Compute — trust FC, permit OSS read + SLS write
# Uses the non-deprecated fields (role_name, assume_role_policy_document).
# ---------------------------------------------------------------------------
resource "alicloud_ram_role" "fc_execution" {
  role_name                  = "${local.name_prefix}-fc-execution"
  description                = "Execution role assumed by the WASPADA Function Compute app."
  assume_role_policy_document = <<JSON
{
  "Version": "1",
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": ["fc.aliyuncs.com"]
      }
    }
  ]
}
JSON
}

# Allow FC to read from the OSS bucket (loans.parquet)
resource "alicloud_ram_policy" "fc_oss_read" {
  policy_name     = "${local.name_prefix}-fc-oss-read"
  policy_document = <<JSON
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "oss:GetObject",
        "oss:GetObjectMeta",
        "oss:ListObjects",
        "oss:GetBucketInfo"
      ],
      "Resource": [
        "acs:oss:*:*:${alicloud_oss_bucket.loans.bucket}",
        "acs:oss:*:*:${alicloud_oss_bucket.loans.bucket}/*"
      ]
    }
  ]
}
JSON
}

resource "alicloud_ram_role_policy_attachment" "fc_oss_read" {
  role_name   = alicloud_ram_role.fc_execution.role_name
  policy_name = alicloud_ram_policy.fc_oss_read.policy_name
  policy_type = "Custom"
}

# Allow FC to write audit logs to SLS
resource "alicloud_ram_policy" "fc_sls_write" {
  policy_name     = "${local.name_prefix}-fc-sls-write"
  policy_document = <<JSON
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "log:PostLogStoreLogs",
        "log:CreateLogStore",
        "log:GetLogStore",
        "log:ListLogStores",
        "log:CreateIndex",
        "log:UpdateIndex",
        "log:GetIndex"
      ],
      "Resource": "acs:log:*:*:project/${alicloud_log_project.audit.project_name}/logstore/${alicloud_log_store.audit.logstore_name}"
    }
  ]
}
JSON
}

resource "alicloud_ram_role_policy_attachment" "fc_sls_write" {
  role_name   = alicloud_ram_role.fc_execution.role_name
  policy_name = alicloud_ram_policy.fc_sls_write.policy_name
  policy_type = "Custom"
}

# ---------------------------------------------------------------------------
# Simple Log Service — audit stream (WA-023)
# Uses the non-deprecated field names (project_name / logstore_name).
# ---------------------------------------------------------------------------
resource "alicloud_log_project" "audit" {
  project_name = "${local.name_prefix}-audit"
  description  = "WASPADA audit log stream (API access + scoring events)."
  tags = {
    Project     = var.namespace
    Environment = var.environment
    ManagedBy   = "opentofu"
  }
}

resource "alicloud_log_store" "audit" {
  project_name           = alicloud_log_project.audit.project_name
  logstore_name          = "audit"
  retention_period       = 90 # days
  shard_count            = 2
  auto_split             = true
  max_split_shard_count  = 4
  append_meta            = true
}

# --------------------------------------------------------------------------- #
# ApsaraDB RDS PostgreSQL — user store for auth (WA-028)
# 5th Alibaba Cloud service. Cheapest instance type for the demo.
# ---------------------------------------------------------------------------
resource "alicloud_db_instance" "auth" {
  engine               = "PostgreSQL"
  engine_version       = "15.0"
  instance_type        = var.rds_instance_type
  instance_storage     = "20"
  instance_name        = "${local.name_prefix}-auth-db"
  instance_charge_type = "Postpaid"

  security_ips = ["0.0.0.0/0"]

  tags = {
    Project     = var.namespace
    Environment = var.environment
    ManagedBy   = "opentofu"
    Component   = "rds-auth"
  }
}

resource "alicloud_db_database" "auth" {
  instance_id = alicloud_db_instance.auth.id
  name        = "waspada"
}

resource "alicloud_rds_account" "auth" {
  db_instance_id   = alicloud_db_instance.auth.id
  account_name     = "waspada"
  account_password = var.rds_password
  account_type     = "Normal"
}

# ---------------------------------------------------------------------------
# Function Compute 3.0 — custom-container, CAPort 8080, serves api/main.py
# ---------------------------------------------------------------------------
resource "alicloud_fcv3_function" "api" {
  function_name        = "${local.name_prefix}-api"
  runtime              = "custom-container"
  handler              = "index.handler"
  memory_size          = 2048
  timeout              = 60
  cpu                  = 1.0
  disk_size            = 512
  instance_concurrency = 10

  custom_container_config {
    image   = local.fc_image
    command = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
    port    = 8080 # matches Dockerfile EXPOSE + CAPort env
    health_check_config {
      http_get_url = "/api/health"
    }
  }

  role = alicloud_ram_role.fc_execution.arn

  log_config {
    project                = alicloud_log_project.audit.project_name
    logstore               = alicloud_log_store.audit.logstore_name
    enable_request_metrics = true
    enable_instance_metrics = false
  }

  environment_variables = {
    CAPort           = "8080"
    PYTHONPATH       = "/app"
    PYTHONUNBUFFERED = "1"
  }

  tags = {
    Project     = var.namespace
    Environment = var.environment
    ManagedBy   = "opentofu"
    Component   = "fc-api"
  }
}

# ---------------------------------------------------------------------------
# HTTP trigger — public *.fcapp.run URL
# trigger_config is a JSON string (FC 3.0 trigger API shape).
# ---------------------------------------------------------------------------
resource "alicloud_fcv3_trigger" "http" {
  function_name = alicloud_fcv3_function.api.function_name
  trigger_name  = "${local.name_prefix}-http"
  trigger_type  = "http"
  qualifier     = "LATEST"
  trigger_config = jsonencode({
    authType = "anonymous" # public read; switch to "function" for signed URLs
    methods  = ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]
  })
}
