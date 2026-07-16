output "fc_url" {
  description = "Public Function Compute endpoint (fcapp.run). Append /api/health to verify."
  # http_trigger is a computed list of objects with url_internet.
  value = try("https://${alicloud_fcv3_trigger.http.http_trigger[0].url_internet}", "pending-deploy")
}

output "fc_function_name" {
  description = "Name of the FC 3.0 function."
  value       = alicloud_fcv3_function.api.function_name
}

output "oss_raw_bucket_name" {
  description = "OSS Raw (Bronze) bucket — immutable source (loans.parquet)."
  value       = alicloud_oss_bucket.raw.bucket
}

output "oss_staging_bucket_name" {
  description = "OSS Staging (Silver) bucket — curated lane views."
  value       = alicloud_oss_bucket.staging.bucket
}

output "oss_mart_bucket_name" {
  description = "OSS Mart (Gold) bucket — serving layer for agents + dashboard."
  value       = alicloud_oss_bucket.mart.bucket
}

output "oss_endpoint_internal" {
  description = "OSS internal endpoint (use from within Alibaba Cloud / FC). All three buckets share the same endpoint."
  value       = alicloud_oss_bucket.raw.intranet_endpoint
}

output "oss_endpoint_public" {
  description = "OSS public endpoint. All three buckets share the same endpoint."
  value       = alicloud_oss_bucket.raw.extranet_endpoint
}

output "acr_registry_domain" {
  description = "ACR registry domain — push images here in the WA-018 deploy step."
  value       = var.acr_registry_domain
}

output "acr_repo" {
  description = "Full ACR repository path (image push target). Namespace/repo are managed manually in the console."
  value       = "${var.acr_registry_domain}/${local.acr_namespace}/api"
}

output "sls_project" {
  description = "Simple Log Service project name (audit stream)."
  value       = alicloud_log_project.audit.project_name
}

output "sls_logstore" {
  description = "Simple Log Service logstore name."
  value       = alicloud_log_store.audit.logstore_name
}

output "fc_execution_role_arn" {
  description = "ARN of the FC execution role."
  value       = alicloud_ram_role.fc_execution.arn
}

output "rds_connection_string" {
  description = "RDS PostgreSQL connection string (for DATABASE_URL)."
  value       = alicloud_db_instance.auth.connection_string
}

output "vpc_id" {
  description = "ID of the managed VPC (RDS + FC live inside it)."
  value       = alicloud_vpc.main.id
}

output "rds_port" {
  description = "RDS PostgreSQL port."
  value       = alicloud_db_instance.auth.port
}

output "rds_database_url" {
  description = "Full DATABASE_URL for the app (postgres://user:pass@host:port/db). Constructed from RDS outputs."
  value       = "postgres://waspada:${var.rds_password}@${alicloud_db_instance.auth.connection_string}:${alicloud_db_instance.auth.port}/waspada"
  sensitive   = true
}
