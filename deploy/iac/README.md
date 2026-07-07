# WA-027 · Alibaba Cloud IaC (OpenTofu)

Declarative infrastructure for WASPADA on Alibaba Cloud — OSS + ACR + Function
Compute 3.0 + Simple Log Service + RAM. Region: `ap-southeast-1` (Singapore,
international free tier).

The owner runs `tofu apply` (real-spend gate). This config is **code only** — it
does not auto-apply in CI.

## Prerequisites

1. **OpenTofu** ≥ 1.6 — install from <https://opentofu.org>.
2. **Alibaba Cloud AccessKey** with permissions to manage OSS, ACR (Personal),
   Function Compute, SLS, and RAM. Create one at
   <https://ram.console.aliyun.com/users> → create AccessKey.
3. **`aliyun` CLI** (optional, for the console fallback below):
   <https://www.alibabacloud.com/help/en/product/29991>.

## Credentials — never commit

Two equivalent options:

### Option A — environment variables (recommended for one-off runs)

```bash
export TF_VAR_access_key="LTAI..."
export TF_VAR_secret_key="..."
cd deploy/iac
tofu init
tofu plan
```

### Option B — secrets.tfvars (gitignored)

```bash
cp secrets.tfvars.example secrets.tfvars
# edit secrets.tfvars with your real AccessKey + secret
cd deploy/iac
tofu init
tofu plan  -var-file=secrets.tfvars
tofu apply -var-file=secrets.tfvars
```

`secrets.tfvars`, `*.tfstate*`, and `.terraform/` are all gitignored — verify
before pushing:

```bash
git check-ignore deploy/iac/secrets.tfvars deploy/iac/terraform.tfstate
# both paths must print back (ignored)
```

## Lifecycle

| Step      | Command                                          |
| --------- | ------------------------------------------------ |
| Init      | `tofu init`                                      |
| Plan      | `tofu plan -var-file=secrets.tfvars`             |
| Apply     | `tofu apply -var-file=secrets.tfvars`            |
| Outputs   | `tofu output`                                    |
| Destroy   | `tofu destroy -var-file=secrets.tfvars`          |

After apply, the FC URL is printed:

```
fc_url = https://<account>.<region>.fcapp.run
```

## Deploying the image (post-apply, owner's WA-018 step)

The IaC creates the ACR repo and FC function, but FC will 502 until a real image
is pushed. From the repo root:

```bash
# 1. Login to ACR (use the registry domain from `tofu output acr_registry_domain`)
docker login registry.ap-southeast-1.aliyuncs.com -u <aliyun-account>

# 2. Build + tag the image
docker build -t registry.ap-southeast-1.aliyuncs.com/<namespace>/api:latest .

# 3. Push
docker push registry.ap-southeast-1.aliyuncs.com/<namespace>/api:latest
```

Then either re-apply or pull a fresh instance of the function. Verify health:

```bash
curl "$(tofu output -raw fc_url)/api/health"
# {"status":"ok"}
```

## Variables

| Name                  | Default                                  | Notes                                                  |
| --------------------- | ---------------------------------------- | ------------------------------------------------------ |
| `region`              | `ap-southeast-1`                         | Singapore (free tier).                                 |
| `access_key`          | *(empty)*                                | via `TF_VAR_access_key` or `secrets.tfvars`. Sensitive.|
| `secret_key`          | *(empty)*                                | via `TF_VAR_secret_key` or `secrets.tfvars`. Sensitive.|
| `namespace`           | `waspada`                                | Prefix for all resource names.                         |
| `environment`         | `prod`                                   | Suffix; goes into resource names.                      |
| `acr_registry_domain` | `registry.ap-southeast-1.aliyuncs.com`   | Personal Edition default; use VPC domain if FC is in a VPC. |
| `fc_image_tag`        | `latest`                                 | ACR image tag the function points at.                  |

## Resources provisioned

| Resource                              | Purpose                                        |
| ------------------------------------- | ---------------------------------------------- |
| `alicloud_oss_bucket.loans`           | loan-portfolio Parquet store (private).        |
| `alicloud_oss_bucket_acl.loans`       | splits ACL out of bucket (new API).            |
| `alicloud_cr_namespace.waspada`       | ACR Personal Edition namespace.                |
| `alicloud_cr_repo.api`                | ACR repo for the FC image.                     |
| `alicloud_ram_role.fc_execution`      | FC trust role.                                 |
| `alicloud_ram_policy.fc_oss_read`     | grant FC read on the OSS bucket.               |
| `alicloud_ram_policy.fc_sls_write`    | grant FC write on the SLS logstore.            |
| `alicloud_ram_role_policy_attachment` | attach each policy to the role (×2).           |
| `alicloud_log_project.audit`          | SLS project (audit stream).                    |
| `alicloud_log_store.audit`            | SLS logstore, 90-day retention.                |
| `alicloud_fcv3_function.api`          | FC 3.0 custom-container function, CAPort 8080. |
| `alicloud_fcv3_trigger.http`          | HTTP trigger → public `*.fcapp.run` URL.       |

## Console / Serverless Devs fallback

If `tofu apply` fails on a specific resource (provider quirk, region mismatch),
the same footprint can be stood up from the Alibaba Cloud console:

- **OSS** → <https://oss.console.aliyun.com> → create bucket `waspada-prod-loans`.
- **ACR** → <https://cr.console.aliyun.com> → Personal → create namespace + repo.
- **FC** → <https://fcnext.console.aliyun.com> → create function (custom-container,
  CAPort 8080, point at ACR image), attach HTTP trigger.
- **SLS** → <https://sls.console.aliyun.com> → create project + logstore.
- **RAM** → <https://ram.console.aliyun.com> → create role for FC + attach the
  same policy documents (they're JSON, copy from `main.tf`).

Or via [Serverless Devs](https://www.serverless-devs.com/) (`s.yaml`) — not
maintained here; OpenTofu is the source of truth.

## Notes / known limitations

- **ACR Personal Edition** resources (`alicloud_cr_namespace`, `alicloud_cr_repo`)
  emit deprecation warnings in favor of Enterprise Edition
  (`alicloud_cr_ee_*`). Personal is free and functional for the hackathon; switch
  to EE if you need VPC-only endpoints or higher quotas.
- **Local state** only — no OSS state backend (intentional; see WA-027 scope).
  `terraform.tfstate*` is gitignored but lives on the operator's disk.
- **Sandbox/MCP** — FC sandbox compatibility with stdio MCP is unverified (see
  `HACKATHON.md` risk register). This IaC serves the FastAPI app; MCP runs
  locally/CI.
