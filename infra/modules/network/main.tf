# VPC for Stage 0: public subnets only, no NAT gateway and no interface endpoints
# (ARCHITECTURE.md, "Stage 0 settings"). The instance reaches AWS APIs, OpenAI and Deepgram
# over the internet gateway with TLS and IAM; its security group admits only the allow-listed
# addresses on port 80. The S3 gateway endpoint is free and keeps upload traffic inside AWS.

variable "name" {
  type = string
}

variable "cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "az_count" {
  type        = number
  default     = 1
  description = "Public subnets, one per zone. Stage 0 runs one instance, so one zone; a load balancer (Stage 1) needs two."
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_region" "current" {}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

# No rules: nothing uses the default group, and it should admit nothing if something does.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false # the instance gets its Elastic IP explicitly
  tags                    = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = { Name = "${var.name}-s3" }
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}
