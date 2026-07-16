# ---------------------------------------------------------------------------
# Local values
# ---------------------------------------------------------------------------
locals {
  name_prefix = "${var.namespace}-${var.environment}"
  # WA-018: ACR namespace was manually created in the Alibaba console as
  # "small-company". Hardcode it instead of deriving from name_prefix.
  acr_namespace = "small-company"
  # Personal Edition ACR default internet domain. Override via var.acr_registry_domain
  # if using Enterprise Edition or a different endpoint.
  fc_image = "${var.acr_registry_domain}/${local.acr_namespace}/api:${var.fc_image_tag}"
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
# OSS buckets — three-bucket medallion architecture (WA-057)
# Raw (Bronze): immutable source Parquet, never rewritten by agents.
# Staging (Silver): lane-specific curated views (FeatureFrame, quality reports).
# Mart (Gold): serving layer — scored accounts, dashboard payloads, audit trail.
# Each bucket is physically isolated: separate RAM policies, lifecycle rules.
# ---------------------------------------------------------------------------

# --- Raw (Bronze) — immutable source -----------------------------------------
resource "alicloud_oss_bucket" "raw" {
  bucket = "${local.name_prefix}-raw"

  tags = {
    Project     = var.namespace
    Environment = var.environment
    ManagedBy   = "opentofu"
    Component   = "raw-bronze"
    Zone        = "raw"
  }
}

resource "alicloud_oss_bucket_acl" "raw" {
  bucket = alicloud_oss_bucket.raw.bucket
  acl    = "private"
}

# --- Staging (Silver) — curated lane views -----------------------------------
resource "alicloud_oss_bucket" "staging" {
  bucket = "${local.name_prefix}-staging"

  tags = {
    Project     = var.namespace
    Environment = var.environment
    ManagedBy   = "opentofu"
    Component   = "staging-silver"
    Zone        = "staging"
  }
}

resource "alicloud_oss_bucket_acl" "staging" {
  bucket = alicloud_oss_bucket.staging.bucket
  acl    = "private"
}

# --- Mart (Gold) — serving layer ---------------------------------------------
resource "alicloud_oss_bucket" "mart" {
  bucket = "${local.name_prefix}-mart"

  tags = {
    Project     = var.namespace
    Environment = var.environment
    ManagedBy   = "opentofu"
    Component   = "mart-gold"
    Zone        = "mart"
  }
}

resource "alicloud_oss_bucket_acl" "mart" {
  bucket = alicloud_oss_bucket.mart.bucket
  acl    = "private"
}

# ---------------------------------------------------------------------------
# ACR Container Registry (Personal Edition, free tier) — hosts the FC image.
# WA-018: The namespace "small-company" and repo "api" were created manually in
# the Alibaba console (Personal Edition ACR). Tofu cannot manage either because
# the provider returns a "jurisdiction error" on read and NAMESPACE_NOT_EXIST
# on create (known ACR Personal Edition cross-region quirk). Both are treated
# as externally managed — the image is pushed via `docker push` directly.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RAM role for Function Compute — trust FC, permit OSS read + SLS write
# Uses the non-deprecated fields (role_name, assume_role_policy_document).
# ---------------------------------------------------------------------------
resource "alicloud_ram_role" "fc_execution" {
  role_name                   = "${local.name_prefix}-fc-execution"
  description                 = "Execution role assumed by the WASPADA Function Compute app."
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

# Allow FC to read from the Raw bucket (immutable source — agents never write here)
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
        "acs:oss:*:*:${alicloud_oss_bucket.raw.bucket}",
        "acs:oss:*:*:${alicloud_oss_bucket.raw.bucket}/*"
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

# Allow FC to write to Staging + Mart (curated views + serving layer)
resource "alicloud_ram_policy" "fc_oss_write" {
  policy_name     = "${local.name_prefix}-fc-oss-write"
  policy_document = <<JSON
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "oss:GetObject",
        "oss:PutObject",
        "oss:DeleteObject",
        "oss:ListObjects",
        "oss:GetBucketInfo"
      ],
      "Resource": [
        "acs:oss:*:*:${alicloud_oss_bucket.staging.bucket}",
        "acs:oss:*:*:${alicloud_oss_bucket.staging.bucket}/*",
        "acs:oss:*:*:${alicloud_oss_bucket.mart.bucket}",
        "acs:oss:*:*:${alicloud_oss_bucket.mart.bucket}/*"
      ]
    }
  ]
}
JSON
}

resource "alicloud_ram_role_policy_attachment" "fc_oss_write" {
  role_name   = alicloud_ram_role.fc_execution.role_name
  policy_name = alicloud_ram_policy.fc_oss_write.policy_name
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
  project_name          = alicloud_log_project.audit.project_name
  logstore_name         = "audit"
  retention_period      = 90 # days
  shard_count           = 2
  auto_split            = true
  max_split_shard_count = 4
  append_meta           = true
}

# --------------------------------------------------------------------------- #
# ApsaraDB RDS PostgreSQL — user store for auth (WA-028)
# 5th Alibaba Cloud service. Cheapest instance type for the demo.
# WA-018: RDS requires a VPC network type — classic network creation is
# unsupported. The RAM user lacks VPC read/create permissions, so the
# vswitch_id must be supplied via a variable. Find your default VSwitch in
# the Alibaba console (VPC > VSwitch) and set var.rds_vswitch_id in tfvars.
# --------------------------------------------------------------------------- #
resource "alicloud_db_instance" "auth" {
  engine               = "PostgreSQL"
  engine_version       = "15.0"
  instance_type        = var.rds_instance_type
  instance_storage     = "20"
  instance_name        = "${local.name_prefix}-auth-db"
  instance_charge_type = "Postpaid"
  # WA-018: VPC network type (classic network is no longer supported)
  vswitch_id = var.rds_vswitch_id
  # WA-044: explicit, documented deletion setting.
  # false = destroy is intentional (run `tofu destroy -target=alicloud_db_instance.auth`).
  # Set true in long-lived prod to prevent accidental data loss.
  deletion_protection = false

  security_ips = var.rds_security_ips

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
  timeout              = 180 # WA-044: live Qwen debate ~70s; 60s killed it mid-debate
  cpu                  = 1.0
  disk_size            = 1024 # WA-044: 512MB was tight for container image + DuckDB + parquet
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
    project                 = alicloud_log_project.audit.project_name
    logstore                = alicloud_log_store.audit.logstore_name
    enable_request_metrics  = true
    enable_instance_metrics = false
  }

  environment_variables = {
    CAPort           = "8080"
    PYTHONPATH       = "/app"
    PYTHONUNBUFFERED = "1"
    # WA-018: runtime credentials for the FC function
    WASPADA_ENV           = "prod"
    DASHSCOPE_API_KEY     = var.dashscope_api_key
    WASPADA_JWT_SECRET    = var.waspada_jwt_secret
    OSS_RAW_BUCKET        = "${local.name_prefix}-raw"
    OSS_STAGING_BUCKET    = "${local.name_prefix}-staging"
    OSS_MART_BUCKET       = "${local.name_prefix}-mart"
    OSS_ENDPOINT          = "oss-ap-southeast-1.aliyuncs.com"
    OSS_KEY               = "loans.parquet"
    OSS_ACCESS_KEY_ID     = var.access_key
    OSS_ACCESS_KEY_SECRET = var.secret_key
    DATABASE_URL          = "postgres://waspada:${var.rds_password}@${alicloud_db_instance.auth.connection_string}:5432/waspada"
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
