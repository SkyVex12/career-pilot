# The ECS cluster. In Stage 0 its only capacity is the host instance (infra/modules/host),
# which registers itself through /etc/ecs/ecs.config; Stage 1 adds Fargate capacity providers.

variable "name" {
  type = string
}

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "disabled" # its metrics cost more than the rest of Stage 0's monitoring
  }
}

output "name" {
  value = aws_ecs_cluster.this.name
}

output "arn" {
  value = aws_ecs_cluster.this.arn
}
