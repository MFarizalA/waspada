output "fc_url" {
  description = "Public Function Compute endpoint (fcapp.run). Append /api/health to verify."
  # http_trigger is a computed list of objects with url_internet.
  value       = try("https://${alicloud_fcv3_trigger.http.http_trigger[0].url_internet}", "pending-deploy")
}

output "fc_function_name" {
  description = "Name of the FC 3.0 function."
  value       = alicloud_fcv3_function.api.function_name
}

output "oss_bucket_name" {
  description = "OSS bucket holding loans.parquet."
  value       = alicloud_oss_bucket.loans.bucket
}

output "oss_endpoint_internal" {
  description = "OSS internal endpoint (use from within Alibaba Cloud / FC)."
  value       = alicloud_oss_bucket.loans.intranet_endpoint
}

output "oss_endpoint_public" {
  description = "OSS public endpoint."
  value       = alicloud_oss_bucket.loans.extranet_endpoint
}

output "acr_registry_domain" {
  description = "ACR registry domain — push images here in the WA-018 deploy step."
  value       = var.acr_registry_domain
}

output "acr_repo" {
  description = "Full ACR repository path (image push target)."
  value       = "${var.acr_registry_domain}/${alicloud_cr_namespace.waspada.name}/${alicloud_cr_repo.api.name}"
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
