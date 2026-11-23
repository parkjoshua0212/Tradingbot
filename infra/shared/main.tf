# Account-wide resources used by both dev and prod: the image repository.
terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {
    bucket       = "tradingbot-tfstate-622056304769"
    key          = "shared/terraform.tfstate"
    region       = "ap-northeast-2"
    use_lockfile = true
  }
}

provider "aws" {
  region = "ap-northeast-2"
}

resource "aws_ecr_repository" "bot" {
  name                 = "tradingbot" # modules/bot reads this name
  image_tag_mutability = "IMMUTABLE"  # a tag always means the same image; Lambda pins the digest anyway

  image_scanning_configuration {
    scan_on_push = true # basic scanning, free
  }
}

resource "aws_ecr_lifecycle_policy" "bot" {
  repository = aws_ecr_repository.bot.name
  # 5, not 2: prod's image survives even if prod lags dev by a few builds (a deleted image breaks the Lambda); layers are shared, so extra cost is tiny.
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the 5 newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

output "repository_url" {
  value = aws_ecr_repository.bot.repository_url
}
