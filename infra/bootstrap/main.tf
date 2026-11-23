# One-time account setup: the $1 budget and the bucket that holds every Terraform state (this one included, after migration).
# No profile here: locally `export AWS_PROFILE=tradingbot`; CI uses OIDC.
terraform {
  required_version = ">= 1.11" # use_lockfile (S3-native locking)
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  # Bucket created by this config with local state first, then state migrated in (init -migrate-state).
  backend "s3" {
    bucket       = "tradingbot-tfstate-622056304769"
    key          = "bootstrap/terraform.tfstate"
    region       = "ap-northeast-2"
    use_lockfile = true
  }
}

provider "aws" {
  region = "ap-northeast-2"
}

variable "alert_email" {
  type        = string
  description = "Budget alert address. Set with `export TF_VAR_alert_email=...` so it never lands in git."
}

data "aws_caller_identity" "me" {}

resource "aws_budgets_budget" "monthly" {
  name         = "tradingbot-monthly"
  budget_type  = "COST"
  limit_amount = "1.0"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

resource "aws_s3_bucket" "tfstate" {
  bucket = "tradingbot-tfstate-${data.aws_caller_identity.me.account_id}" # account id: bucket names are global

  lifecycle {
    prevent_destroy = true # losing this loses track of every resource Terraform manages
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3: free, no KMS key to pay for or manage
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.tfstate.arn, "${aws_s3_bucket.tfstate.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.tfstate] # both edit bucket policy settings; avoid a race
}

resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days           = 90
      newer_noncurrent_versions = 10 # always keep the 10 most recent old versions, however old
    }
  }
  depends_on = [aws_s3_bucket_versioning.tfstate]
}

output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}
