# The uploads bucket (ARCHITECTURE.md, "Data and storage"): documents and the user's own call
# audio under uploads/{user_id}/, uploaded through presigned URLs from the API. Encrypted at
# rest with SSE-KMS (the AWS-managed S3 key, with a bucket key); never public; TLS only.
# Versioning stays off: the user's audio must be gone once it is transcribed, and an old
# version would keep it. The desktop updates bucket comes with signed builds (Stage 1).

variable "name" {
  type = string
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "uploads" {
  bucket        = "${var.name}-uploads-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # tearing down the stage deletes the uploads with it
}

resource "aws_s3_bucket_ownership_controls" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "uploads" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.uploads.arn, "${aws_s3_bucket.uploads.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "uploads" {
  bucket     = aws_s3_bucket.uploads.id
  policy     = data.aws_iam_policy_document.uploads.json
  depends_on = [aws_s3_bucket_public_access_block.uploads]
}

output "bucket" {
  value = aws_s3_bucket.uploads.bucket
}

output "arn" {
  value = aws_s3_bucket.uploads.arn
}
