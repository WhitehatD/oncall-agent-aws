# Who may do what. The three limits on the agent live here:
#
#   1. investigator  the role the model's tools run as: read-only, no data, no secrets
#   2. (policy.py)   the executor's own checks, before any call
#   3. executor      writes only to oncall=managed resources, under a permission
#                    boundary that blocks identity, keys, deletes, code and data
#                    even if its policy is ever widened by mistake

locals {
  logs_arn    = "arn:${data.aws_partition.p.partition}:logs:${var.region}:${local.account}:log-group:/aws/lambda/${local.sys}-*"
  table_arn   = aws_dynamodb_table.incidents.arn
  basic_logs  = "arn:${data.aws_partition.p.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  readonly    = "arn:${data.aws_partition.p.partition}:iam::aws:policy/ReadOnlyAccess"
  managed_tag = { "aws:ResourceTag/oncall" = "managed" }
}

resource "aws_iam_role" "fn" {
  for_each           = toset(["agent", "control", "executor", "approval"])
  name               = "${local.sys}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
  # The executor runs under the boundary below; nothing it is granted can exceed it.
  permissions_boundary = each.key == "executor" ? aws_iam_policy.executor_boundary.arn : null
  tags                 = local.system
}

resource "aws_iam_role_policy_attachment" "fn_logs" {
  for_each   = aws_iam_role.fn
  role       = each.value.name
  policy_arn = local.basic_logs
}

resource "aws_iam_role_policy" "incidents_table" {
  for_each = aws_iam_role.fn
  name     = "incidents-table"
  role     = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
      Resource = local.table_arn
    }]
  })
}

# --- agent: reads the model key and assumes the investigator role. Nothing else. ---

resource "aws_iam_role_policy" "agent" {
  name = "agent"
  role = aws_iam_role.fn["agent"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.api_key.arn },
      { Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.investigator.arn },
    ]
  })
}

# --- investigator: what the model's tools can see ---

resource "aws_iam_role" "investigator" {
  name = "${local.sys}-investigator"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = aws_iam_role.fn["agent"].arn }
    }]
  })
  max_session_duration = 3600
  tags                 = local.system
}

resource "aws_iam_role_policy_attachment" "investigator_readonly" {
  role       = aws_iam_role.investigator.name
  policy_arn = local.readonly
}

resource "aws_iam_role_policy" "investigator_deny" {
  name = "no-data-no-secrets"
  role = aws_iam_role.investigator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "NoDataOrSecrets"
        Effect = "Deny"
        Action = [
          "s3:GetObject*", "s3:SelectObjectContent",
          "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan",
          "dynamodb:PartiQLSelect", "dynamodb:GetRecords",
          "sqs:ReceiveMessage", "kinesis:GetRecords",
          "lambda:GetFunction", "lambda:InvokeFunction",
          "ssm:GetParameter*", "secretsmanager:*", "kms:Decrypt", "kms:GenerateDataKey*",
          # Execution history holds the approval task tokens.
          "states:GetExecutionHistory", "states:DescribeExecution",
        ]
        Resource = "*"
      },
      # ReadOnlyAccess includes no writes, but say it explicitly.
      { Sid = "NoAssume", Effect = "Deny", Action = ["sts:AssumeRole", "iam:PassRole"], Resource = "*" },
    ]
  })
}

# --- executor: writes to managed resources, inside the boundary ---

resource "aws_iam_role_policy" "executor" {
  name = "write-managed"
  role = aws_iam_role.fn["executor"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTaggedManaged"
      Effect    = "Allow"
      Action    = ["lambda:*", "sqs:*", "dynamodb:*", "events:*", "apigateway:*"]
      Resource  = "*"
      Condition = { StringEquals = local.managed_tag }
    }]
  })
}

resource "aws_iam_policy" "executor_boundary" {
  name        = "${local.sys}-executor-boundary"
  description = "The most the on-call executor can ever do, whatever its own policy says"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Ceiling"
        Effect   = "Allow"
        Action   = ["lambda:*", "sqs:*", "dynamodb:*", "events:*", "apigateway:*", "logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"]
        Resource = "*"
      },
      {
        Sid    = "NeverDestroyOrRewire"
        Effect = "Deny"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:UpdateFunctionCode", "lambda:PublishVersion",
          "lambda:DeleteAlias", "lambda:CreateAlias", "lambda:DeleteEventSourceMapping", "lambda:CreateEventSourceMapping",
          "lambda:AddPermission", "lambda:RemovePermission", "lambda:InvokeFunction", "lambda:InvokeFunctionUrl",
          "lambda:CreateFunctionUrlConfig", "lambda:UpdateFunctionUrlConfig", "lambda:DeleteFunctionUrlConfig",
          "lambda:TagResource", "lambda:UntagResource", "lambda:DeleteLayerVersion",
          "sqs:DeleteQueue", "sqs:PurgeQueue", "sqs:CreateQueue", "sqs:SendMessage", "sqs:ReceiveMessage",
          "sqs:DeleteMessage", "sqs:AddPermission", "sqs:RemovePermission", "sqs:TagQueue", "sqs:UntagQueue",
          "dynamodb:DeleteTable", "dynamodb:CreateTable", "dynamodb:DeleteBackup", "dynamodb:RestoreTable*",
          "dynamodb:TagResource", "dynamodb:UntagResource", "dynamodb:PutResourcePolicy", "dynamodb:DeleteResourcePolicy",
          "events:DeleteRule", "events:PutTargets", "events:RemoveTargets", "events:PutPermission",
          "events:RemovePermission", "events:TagResource", "events:UntagResource", "events:PutEvents",
          "apigateway:DELETE",
        ]
        Resource = "*"
      },
      {
        Sid    = "NoDataExceptIncidents"
        Effect = "Deny"
        Action = [
          "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:PutItem",
          "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQL*",
        ]
        NotResource = local.table_arn
      },
    ]
  })
  tags = local.system
}

# --- control and approval ---

resource "aws_iam_role_policy" "control" {
  name = "control"
  role = aws_iam_role.fn["control"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.oncall.arn },
      { Effect = "Allow", Action = "cloudwatch:DescribeAlarms", Resource = "*" },
    ]
  })
}

resource "aws_iam_role_policy" "approval" {
  name = "approval"
  role = aws_iam_role.fn["approval"].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["states:SendTaskSuccess", "states:SendTaskFailure"], Resource = "*" }]
  })
}

# --- Step Functions and EventBridge ---

resource "aws_iam_role" "sfn" {
  name = "${local.sys}-workflow"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "states.amazonaws.com" } }]
  })
  tags = local.system
}

resource "aws_iam_role_policy" "sfn" {
  name = "invoke"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = flatten([for k in ["agent", "control", "executor"] : [aws_lambda_function.sys[k].arn, "${aws_lambda_function.sys[k].arn}:*"]])
    }]
  })
}

resource "aws_iam_role" "events" {
  name = "${local.sys}-alarm-router"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "events.amazonaws.com" } }]
  })
  tags = local.system
}

resource "aws_iam_role_policy" "events" {
  name = "start"
  role = aws_iam_role.events.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "states:StartExecution", Resource = aws_sfn_state_machine.incident.arn }]
  })
}
