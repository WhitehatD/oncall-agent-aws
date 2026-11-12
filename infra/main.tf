terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
    time    = { source = "hashicorp/time", version = "~> 0.12" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "oncall-agent-aws" }
  }
}

data "aws_caller_identity" "me" {}
data "aws_partition" "p" {}

locals {
  account  = data.aws_caller_identity.me.account_id
  build    = "${path.module}/../build"
  managed  = { oncall = "managed" } # the agent may change these
  system   = { oncall = "system" }  # the on-call system itself: never
  sys      = "oncall"               # name prefix of every system resource
  log_days = 7
}
