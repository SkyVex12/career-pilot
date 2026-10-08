# The user-data key (ARCHITECTURE.md, "Terraform layout and IAM": KMS). Envelope encryption:
# one data key per user, made and unwrapped by this key. The key policy refuses every data
# operation that does not name a user_id in its encryption context, so a data key decrypts
# only for the user it was made for, and CloudTrail records whose key was used. The services
# get their own permission through their task roles (with the same condition).

variable "name" {
  type = string
}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "key" {
  statement {
    sid       = "AccountAdministers"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
  statement {
    sid    = "RequireUserContext"
    effect = "Deny"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:GenerateDataKeyWithoutPlaintext",
      "kms:ReEncryptFrom",
      "kms:ReEncryptTo",
    ]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Null"
      variable = "kms:EncryptionContext:user_id"
      values   = ["true"]
    }
  }
}

resource "aws_kms_key" "user_data" {
  description             = "${var.name}: per-user data keys (encryption context user_id)"
  key_usage               = "ENCRYPT_DECRYPT"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.key.json
}

resource "aws_kms_alias" "user_data" {
  name          = "alias/${var.name}-user-data"
  target_key_id = aws_kms_key.user_data.key_id
}

output "key_arn" {
  value = aws_kms_key.user_data.arn
}

output "alias" {
  value = aws_kms_alias.user_data.name
}
