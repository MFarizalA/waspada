# WA-018: grant the auth account access to its database.
#
# main.tf creates the `waspada` account (account_type = "Normal") and the
# `waspada` database, but never linked them — so the app boots, connects to RDS,
# and crashes with:
#   pymysql OperationalError (1044, "Access denied for user 'waspada'@'%' to
#   database 'waspada'")
#
# A raw `GRANT` won't fix this: a "Normal" account can't grant to itself and
# Alibaba RDS locks the high-privilege/root account. The privilege is granted
# through the RDS API (GrantAccountPrivilege) — which is exactly what this
# resource does, using the AccessKey rather than a MySQL session.
resource "alicloud_db_account_privilege" "auth" {
  instance_id  = alicloud_db_instance.auth.id
  account_name = alicloud_rds_account.auth.account_name
  privilege    = "ReadWrite"
  db_names     = [alicloud_db_database.auth.name]
}
