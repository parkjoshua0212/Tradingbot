# Everything one environment (dev or prod) needs: state bucket, table, secrets, Lambda and its role.
# Prod-only pieces (Scheduler, ERROR alarm, SNS) are added in M4.
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "env" {
  type = string
  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be dev or prod."
  }
}

variable "dry_run" {
  type        = string
  default     = null
  description = "Only \"0\" places orders. Leave null (dev) so DRY_RUN is unset and the handler fails safe to a dry run."
}

data "aws_caller_identity" "me" {}
data "aws_region" "current" {}

locals {
  name       = "tradingbot-${var.env}"
  param_path = "/tradingbot/${var.env}"
  secrets    = toset(["KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET", "KIS_MOCK_ACCOUNT", "KIS_MOCK_PRODUCT_CODE", "DISCORD_WEBHOOK_URL"])
}

# --- state files (state.json, last_run.txt, KILL, results/*.csv) ---
resource "aws_s3_bucket" "state" {
  bucket = "${local.name}-state-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled" # state.json is the ledger: a bad write can be rolled back
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.state]
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days           = 90
      newer_noncurrent_versions = 10
    }
  }
  depends_on = [aws_s3_bucket_versioning.state]
}

# --- once-per-day guard (RUN#<date>) and order copies (ORDER#<odno>) ---
resource "aws_dynamodb_table" "bot" {
  name                        = local.name
  billing_mode                = "PROVISIONED" # the always-free 25 RCU/WCU covers provisioned mode only
  read_capacity               = 1
  write_capacity              = 1 # handler writes only new or changed orders
  hash_key                    = "pk"
  deletion_protection_enabled = var.env == "prod"

  attribute {
    name = "pk"
    type = "S"
  }
}

# --- secrets: Terraform creates the names; you set the values with put-parameter --overwrite ---
resource "aws_ssm_parameter" "secret" {
  for_each         = local.secrets
  name             = "${local.param_path}/${each.key}"
  type             = "SecureString" # AWS-managed aws/ssm key: free, and its key policy already lets this account decrypt via SSM
  value_wo         = "set-me"       # write-only: never stored in state, so real values set later never land there either
  value_wo_version = 1              # only a bump re-sends the placeholder; your overwrites are left alone
}

# --- Lambda ---
data "aws_ecr_image" "bot" {
  repository_name = "tradingbot" # infra/shared
  most_recent     = true
}

resource "aws_cloudwatch_log_group" "bot" {
  name              = "/aws/lambda/${local.name}" # created before the function so retention applies from the first run
  retention_in_days = 7
}

resource "aws_iam_role" "bot" {
  name = local.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "bot" {
  name = "run"
  role = aws_iam_role.bot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.bot.arn}:*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.state.arn}/*" },
      # without ListBucket a missing file is 403, not 404, and the handler's "first run, no KILL" path would crash
      { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.state.arn },
      { Effect = "Allow", Action = "dynamodb:PutItem", Resource = aws_dynamodb_table.bot.arn },
      { Effect = "Allow", Action = "ssm:GetParametersByPath", Resource = "arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.me.account_id}:parameter${local.param_path}" },
    ]
  })
}

resource "aws_lambda_function" "bot" {
  function_name = local.name
  role          = aws_iam_role.bot.arn
  package_type  = "Image"
  image_uri     = data.aws_ecr_image.bot.image_uri # by digest: a new push changes the plan, a re-tag cannot
  architectures = ["arm64"]
  memory_size   = 1024 # guess (pandas import); set from "Max Memory Used" after the first invoke
  timeout       = 300  # guess: ~10 orders at 0.6 s + a 10 s fill poll + data fetch; set from "Duration"

  environment {
    variables = merge(
      { STATE_BUCKET = aws_s3_bucket.state.bucket, TABLE_NAME = aws_dynamodb_table.bot.name, PARAM_PATH = local.param_path },
      var.dry_run == null ? {} : { DRY_RUN = var.dry_run },
    )
  }

  depends_on = [aws_cloudwatch_log_group.bot, aws_iam_role_policy.bot]
}

resource "aws_lambda_function_event_invoke_config" "bot" {
  function_name          = aws_lambda_function.bot.function_name
  maximum_retry_attempts = 0 # a retried run could place orders twice; the date guard is the backstop, not the plan
}

output "function_name" {
  value = aws_lambda_function.bot.function_name
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}
