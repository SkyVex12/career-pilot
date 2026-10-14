# One repository per image (ARCHITECTURE.md, "CI/CD"): ARM64 images tagged with the git SHA.
# Tags are immutable, so a SHA always means the same image and a rollback redeploys exactly
# what ran before. The last `keep` images stay; older ones expire.

variable "name" {
  type = string
}

variable "repositories" {
  type = set(string)
}

variable "keep" {
  type    = number
  default = 15
}

resource "aws_ecr_repository" "this" {
  for_each             = var.repositories
  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # tearing down the stage removes its images too
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last ${var.keep} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.keep
      }
      action = { type = "expire" }
    }]
  })
}

output "urls" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "arns" {
  value = { for k, r in aws_ecr_repository.this : k => r.arn }
}
