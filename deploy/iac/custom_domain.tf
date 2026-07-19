# WA-067 — custom domain so the dashboard RENDERS instead of downloading.
#
# WHY: the default *.fcapp.run test domain injects `Content-Disposition:
# attachment` on every text/html response (shared-domain anti-abuse), so the
# live dashboard "downloads" instead of rendering. Alibaba's own guidance is to
# bind a custom domain for production. A custom domain removes the forced
# download because it's our domain, not the shared one.
#
# COST: FC custom domain + Alibaba Cloud DNS basic records + a free Alibaba DV
# certificate are all FREE. We deliberately do NOT use ALB or Cloud-native API
# Gateway — those bill hourly / per-request.
#
# SCOPE: this file adds exactly TWO resources (the custom domain + one DNS
# record) plus a data source. It does not modify the FC function, its trigger,
# RDS, OSS, or SLS. Apply scoped:
#   tofu apply -target=alicloud_fcv3_custom_domain.app \
#              -target=alicloud_alidns_record.app -var-file=secrets.tfvars
# The plan MUST read "2 to add, 0 to change, 0 to destroy" — if it wants to
# change/replace the function or trigger, STOP.
#
# APPLY ORDER: waspada.xyz must pass Alibaba real-name verification first, or the
# DNS record create fails.

variable "custom_domain" {
  type        = string
  default     = "app.waspada.xyz"
  description = "Custom domain fronting the FC function (inline HTML render)."
}

# CNAME target for the custom domain. Empty -> derive <uid>.<region>.fc.aliyuncs.com.
# After the custom domain is created, confirm this against what the FC console
# shows and override with -var if the format differs.
variable "fc_cname_target" {
  type        = string
  default     = ""
  description = "CNAME target from the FC custom-domain console (overrides the derived value)."
}

# HTTPS toggle (kept separate from the cert PEMs so the URL output stays
# non-sensitive). HTTP-first bring-up = false; set true AND supply both cert vars
# to serve HTTPS with a free Alibaba DV cert. Never commit the private key —
# pass the PEMs via -var or a gitignored *.auto.tfvars.
variable "enable_https" {
  type        = bool
  default     = false
  description = "Serve HTTPS (requires cert_certificate + cert_private_key). false = HTTP only."
}

variable "cert_certificate" {
  type        = string
  default     = ""
  sensitive   = true
  description = "PEM certificate chain for HTTPS (free Alibaba DV cert)."
}

variable "cert_private_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "PEM private key for the HTTPS certificate."
}

locals {
  https_enabled = var.enable_https
}

data "alicloud_account" "current" {}

# FC 3.0 custom domain bound to the API function.
resource "alicloud_fcv3_custom_domain" "app" {
  custom_domain_name = var.custom_domain
  # HTTP for first bring-up; flips to HTTP,HTTPS automatically once a cert is set.
  protocol = local.https_enabled ? "HTTP,HTTPS" : "HTTP"

  route_config {
    routes {
      path          = "/*"
      function_name = alicloud_fcv3_function.api.function_name
      qualifier     = "LATEST"
      methods       = ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]
    }
  }

  # Free Alibaba DV cert, wired only when provided (keeps the file HTTP-valid).
  dynamic "cert_config" {
    for_each = local.https_enabled ? [1] : []
    content {
      cert_name   = "waspada-app"
      certificate = var.cert_certificate
      private_key = var.cert_private_key
    }
  }

  lifecycle {
    precondition {
      condition     = !var.enable_https || (var.cert_certificate != "" && var.cert_private_key != "")
      error_message = "enable_https = true requires both cert_certificate and cert_private_key (free Alibaba DV cert PEMs)."
    }
  }
}

# Alibaba Cloud DNS: app.waspada.xyz CNAME -> FC region endpoint (free).
resource "alicloud_alidns_record" "app" {
  domain_name = "waspada.xyz"
  rr          = "app"
  type        = "CNAME"
  value       = var.fc_cname_target != "" ? var.fc_cname_target : "${data.alicloud_account.current.id}.${var.region}.fc.aliyuncs.com"
  ttl         = 600
}

output "custom_domain_url" {
  value       = "${local.https_enabled ? "https" : "http"}://${var.custom_domain}"
  description = "The dashboard URL that renders inline (no forced download)."
}
