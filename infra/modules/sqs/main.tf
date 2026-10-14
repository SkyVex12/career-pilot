# The jobs queue and its dead-letter queue (ARCHITECTURE.md, "SQS job catalog"): one standard
# queue; producers set MessageGroupId to the user id, which makes it a fair queue. A message
# goes to the dead-letter queue after 5 receives.

variable "name" {
  type = string
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-jobs-dlq"
  message_retention_seconds = 14 * 24 * 3600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${var.name}-jobs"
  visibility_timeout_seconds = 300 # the worker sets each job type's own on receive
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 4 * 24 * 3600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

output "jobs_url" {
  value = aws_sqs_queue.jobs.url
}

output "jobs_arn" {
  value = aws_sqs_queue.jobs.arn
}

output "dlq_arn" {
  value = aws_sqs_queue.dlq.arn
}

output "dlq_name" {
  value = aws_sqs_queue.dlq.name
}
