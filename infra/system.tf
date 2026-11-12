# The on-call system: alarm -> EventBridge -> Step Functions -> agent / executor / control,
# with an approval page behind an HTTP API. Tagged oncall=system and named oncall-*,
# so neither the executor's IAM nor its checks will touch any of it.

resource "aws_dynamodb_table" "incidents" {
  name         = "${local.sys}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  tags = local.system
}

resource "aws_ssm_parameter" "api_key" {
  name  = "/${local.sys}/openrouter-api-key"
  type  = "SecureString"
  value = var.openrouter_api_key
  tags  = local.system
}

resource "aws_sns_topic" "oncall" {
  name = "${local.sys}-notifications"
  tags = local.system
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.oncall.arn
  protocol  = "email"
  endpoint  = var.email
}

# --- functions ---

locals {
  sys_functions = {
    agent    = { timeout = 600, memory = 512, handler = "agent.handler.handler" }
    control  = { timeout = 30, memory = 256, handler = "control.handler.handler" }
    executor = { timeout = 30, memory = 256, handler = "executor.handler.handler" }
    approval = { timeout = 10, memory = 256, handler = "approval.handler.handler" }
  }
  common_env = {
    INCIDENTS_TABLE = aws_dynamodb_table.incidents.name
    SYSTEM_PREFIX   = "${local.sys}-"
    MAX_ATTEMPTS    = tostring(var.max_attempts)
    MAX_WRITES      = tostring(var.max_attempts)
  }
  sys_env = {
    agent = {
      MODEL                 = var.model
      API_KEY_PARAM         = aws_ssm_parameter.api_key.name
      INVESTIGATOR_ROLE_ARN = aws_iam_role.investigator.arn
      MAX_TOOL_CALLS        = tostring(var.max_tool_calls)
      MAX_COST_USD          = tostring(var.max_cost_usd)
      PRICE_IN_PER_M        = tostring(var.price_in_per_m)
      PRICE_OUT_PER_M       = tostring(var.price_out_per_m)
    }
    control = {
      TOPIC_ARN      = aws_sns_topic.oncall.arn
      APPROVAL_URL   = aws_apigatewayv2_api.approval.api_endpoint
      APPROVAL_TTL_S = tostring(var.approval_timeout_s)
    }
    executor = {}
    approval = {}
  }
}

data "archive_file" "sys" {
  for_each    = local.sys_functions
  type        = "zip"
  source_dir  = "${local.build}/${each.key}"
  output_path = "${local.build}/zip/${each.key}.zip"
}

resource "aws_cloudwatch_log_group" "sys" {
  for_each          = local.sys_functions
  name              = "/aws/lambda/${local.sys}-${each.key}"
  retention_in_days = local.log_days
  tags              = local.system
}

resource "aws_lambda_function" "sys" {
  for_each         = local.sys_functions
  function_name    = "${local.sys}-${each.key}"
  role             = aws_iam_role.fn[each.key].arn
  runtime          = "python3.12"
  handler          = each.value.handler
  filename         = data.archive_file.sys[each.key].output_path
  source_code_hash = data.archive_file.sys[each.key].output_base64sha256
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  environment {
    variables = merge(local.common_env, local.sys_env[each.key])
  }
  tags       = local.system
  depends_on = [aws_cloudwatch_log_group.sys, aws_iam_role_policy_attachment.fn_logs]
}

# --- the workflow ---

resource "aws_sfn_state_machine" "incident" {
  name     = "${local.sys}-incident"
  role_arn = aws_iam_role.sfn.arn
  definition = templatefile("${path.module}/statemachine.asl.json", {
    control_arn        = aws_lambda_function.sys["control"].arn
    agent_arn          = aws_lambda_function.sys["agent"].arn
    executor_arn       = aws_lambda_function.sys["executor"].arn
    max_attempts       = var.max_attempts
    approval_timeout_s = var.approval_timeout_s
    verify_wait_s      = var.verify_wait_s
    verify_polls       = var.verify_polls
  })
  tags = local.system
}

# Only alarms on the demo service start incidents, and only when they go into ALARM.
resource "aws_cloudwatch_event_rule" "alarms" {
  name        = "${local.sys}-alarm-to-incident"
  description = "Starts the incident workflow when a demo-* alarm goes into ALARM"
  event_pattern = jsonencode({
    source      = ["aws.cloudwatch"]
    detail-type = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [{ prefix = "demo-" }]
      state     = { value = ["ALARM"] }
    }
  })
  tags = local.system
}

resource "aws_cloudwatch_event_target" "alarms" {
  rule     = aws_cloudwatch_event_rule.alarms.name
  arn      = aws_sfn_state_machine.incident.arn
  role_arn = aws_iam_role.events.arn
}

# --- the approval page ---

resource "aws_apigatewayv2_api" "approval" {
  name          = "${local.sys}-approval"
  protocol_type = "HTTP"
  tags          = local.system
}

resource "aws_apigatewayv2_integration" "approval" {
  api_id                 = aws_apigatewayv2_api.approval.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.sys["approval"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "approval" {
  for_each  = toset(["GET /d/{id}", "POST /d/{id}"])
  api_id    = aws_apigatewayv2_api.approval.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.approval.id}"
}

resource "aws_apigatewayv2_stage" "approval" {
  api_id      = aws_apigatewayv2_api.approval.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 2
  }
  tags = local.system
}

resource "aws_lambda_permission" "approval" {
  statement_id  = "apigw"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sys["approval"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.approval.execution_arn}/*/*"
}
