output "api_url" {
  description = "The desktop app's AWS API URL"
  value       = local.api_url
}

output "gateway_url" {
  description = "The desktop app's AWS gateway URL"
  value       = local.gateway_url
}

output "public_dns" {
  value = local.public_dns
}

output "instance_id" {
  value = module.host.instance_id
}

output "data_volume_id" {
  value = module.host.data_volume_id
}

output "cluster" {
  value = module.cluster.name
}

output "cognito" {
  value = {
    user_pool_id = module.cognito.user_pool_id
    client_id    = module.cognito.client_id
    domain       = module.cognito.domain_url
  }
}

output "secrets" {
  description = "Fill openai and deepgram by hand (README.md: Deploying Stage 0); database is generated"
  value = {
    openai   = module.secrets.openai_arn
    deepgram = module.secrets.deepgram_arn
    database = module.secrets.database_arn
  }
}

output "ecr" {
  value = module.ecr.urls
}

output "image_tag" {
  value = local.image_tag
}

output "image_tag_parameter" {
  value = aws_ssm_parameter.image_tag.name
}

output "deploy" {
  description = "What infra/scripts/deploy.py needs"
  value = {
    region          = var.region
    cluster         = module.cluster.name
    services        = ["proxy", "gateway", "api", "worker"]
    families        = { for m in [module.proxy, module.gateway, module.api, module.worker, module.migrate, module.scheduler] : m.family => m.family }
    migrate_family  = module.migrate.family
    image_parameter = aws_ssm_parameter.image_tag.name
    ecr             = module.ecr.urls
  }
}

output "ci_roles" {
  value = {
    plan   = module.ci.plan_role_arn
    deploy = module.ci.deploy_role_arn
  }
}

output "log_groups" {
  value = local.all_log_groups
}

output "jobs_dlq" {
  value = module.sqs.dlq_name
}
