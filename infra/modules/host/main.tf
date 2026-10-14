# The Stage 0 instance: one ARM64 EC2 instance in the ECS cluster that runs every container
# (gateway, API, worker, scheduler, migrations, the proxy, Postgres and Valkey), with host
# networking. Its Elastic IP gives it a stable public DNS name; the security group admits port
# 80 from the allow-list only. No SSH: Session Manager and ECS Exec instead.
#
# Postgres and Valkey keep their data on a separate EBS volume, which survives instance stops,
# redeploys and an instance replacement, and is snapshotted daily. Stopping the instance stops
# the whole app; only EBS, the Elastic IP and small fixed items bill while it is stopped.

variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "instance_type" {
  type    = string
  default = "t4g.small"
}

variable "allowed_cidrs" {
  type        = list(string)
  description = "Who may reach port 80 (the app has no TLS in Stage 0)"
}

variable "root_volume_gb" {
  type    = number
  default = 30 # the ECS-optimized AMI's minimum
}

variable "data_volume_gb" {
  type    = number
  default = 10
}

variable "swap_mb" {
  type    = number
  default = 1024
}

variable "snapshot_days" {
  type    = number
  default = 7
}

variable "final_snapshot" {
  type        = bool
  default     = true
  description = "Snapshot the data volume when it is destroyed (terraform destroy keeps one copy)"
}

data "aws_subnet" "this" {
  id = var.subnet_id
}

data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
}

# --- Security group --------------------------------------------------------------------------

resource "aws_security_group" "host" {
  name        = "${var.name}-host"
  description = "Stage 0 instance: port 80 from the allow-list only"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-host" }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each          = toset(var.allowed_cidrs)
  security_group_id = aws_security_group.host.id
  description       = "allow-listed client"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.host.id
  description       = "AWS APIs, OpenAI, Deepgram, image pulls"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# --- Instance role: the ECS agent, the log driver and Session Manager; never the app ---------

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "host" {
  name               = "${var.name}-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "ecs" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "host" {
  name = "${var.name}-host"
  role = aws_iam_role.host.name
}

# --- Data volume -----------------------------------------------------------------------------

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.this.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  final_snapshot    = var.final_snapshot
  tags              = { Name = "${var.name}-data", Snapshot = "${var.name}-daily" }
}

# --- Instance --------------------------------------------------------------------------------

resource "aws_instance" "host" {
  ami                    = data.aws_ssm_parameter.ami.value
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile   = aws_iam_instance_profile.host.name
  monitoring             = false

  # Standard credits: a runaway process is throttled to the baseline instead of billed.
  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    cluster        = var.cluster_name
    data_volume_id = aws_ebs_volume.data.id
    swap_mb        = var.swap_mb
  })
  user_data_replace_on_change = false

  tags = { Name = "${var.name}-host" }

  lifecycle {
    # A new ECS AMI does not replace the instance on its own; see infra/README.md for the
    # update steps (the data volume moves to the new instance).
    ignore_changes = [ami, user_data]
  }
}

resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.host.id
  stop_instance_before_detaching = true
}

resource "aws_eip" "host" {
  domain   = "vpc"
  instance = aws_instance.host.id
  tags     = { Name = "${var.name}-host" }
}

# --- Daily snapshots of the data volume (automated backups, not cut at any stage) -----------

data "aws_iam_policy_document" "dlm_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dlm.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dlm" {
  name               = "${var.name}-dlm"
  assume_role_policy = data.aws_iam_policy_document.dlm_trust.json
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "data" {
  description        = "${var.name}: daily data volume snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"
  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Snapshot = "${var.name}-daily" }
    schedule {
      name      = "daily"
      copy_tags = true
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["06:00"]
      }
      retain_rule {
        count = var.snapshot_days
      }
    }
  }
}

output "instance_id" {
  value = aws_instance.host.id
}

output "public_dns" {
  value = aws_eip.host.public_dns
}

output "public_ip" {
  value = aws_eip.host.public_ip
}

output "security_group_id" {
  value = aws_security_group.host.id
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}
