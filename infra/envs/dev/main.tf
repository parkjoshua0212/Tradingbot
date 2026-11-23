# Dev: always a dry run (DRY_RUN unset), no schedule. Invoke by hand.
terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {
    bucket       = "tradingbot-tfstate-622056304769"
    key          = "dev/terraform.tfstate"
    region       = "ap-northeast-2"
    use_lockfile = true
  }
}

provider "aws" {
  region = "ap-northeast-2"
}

module "bot" {
  source = "../../modules/bot"
  env    = "dev"
}

output "function_name" {
  value = module.bot.function_name
}

output "state_bucket" {
  value = module.bot.state_bucket
}
