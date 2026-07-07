terraform {
  required_version = ">= 1.6.0" # OpenTofu is terraform-syntax compatible

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.236"
    }
  }
}
