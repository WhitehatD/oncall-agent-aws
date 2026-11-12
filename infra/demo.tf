# The demo "orders" service: the thing that breaks. Everything here is tagged
# oncall=managed, which is the only tag the executor may write to.
#
#   traffic (every minute) -> HTTP API -> orders-api:live -> DynamoDB + SQS -> orders-worker
#                                                                       \-> DLQ after 3 failures

locals {
  demo_functions = {
    api     = { name = "demo-orders-api", timeout = 5, memory = 128 }
    worker  = { name = "demo-orders-worker", timeout = 10, memory = 256 }
    traffic = { name = "demo-traffic", timeout = 30, memory = 128 }
  }
}

resource "aws_dynamodb_table" "orders" {
  name           = "demo-orders"
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5
  hash_key       = "order_id"
  attribute {
    name = "order_id"
    type = "S"
  }
  tags = local.managed
}

resource "aws_sqs_queue" "dlq" {
  name                      = "demo-orders-dlq"
  message_retention_seconds = 1209600
  tags                      = local.managed
}

resource "aws_sqs_queue" "orders" {
  name                       = "demo-orders"
  visibility_timeout_seconds = 60
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dlq.arn, maxReceiveCount = 3 })
  tags                       = local.managed
}

# --- roles ---

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "demo" {
  for_each           = local.demo_functions
  name               = "${each.value.name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
  tags               = local.managed
}

resource "aws_iam_role_policy_attachment" "demo_logs" {
  for_each   = local.demo_functions
  role       = aws_iam_role.demo[each.key].name
  policy_arn = "arn:${data.aws_partition.p.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "api" {
  name = "app"
  role = aws_iam_role.demo["api"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = aws_dynamodb_table.orders.arn },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.orders.arn },
    ]
  })
}

# The missing_permission scenario rewrites this policy without UpdateItem.
resource "aws_iam_role_policy" "worker" {
  name = "app"
  role = aws_iam_role.demo["worker"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:UpdateItem"], Resource = aws_dynamodb_table.orders.arn },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
        Resource = aws_sqs_queue.orders.arn
      },
    ]
  })
}

# --- functions ---

data "archive_file" "demo" {
  for_each    = local.demo_functions
  type        = "zip"
  source_dir  = "${local.build}/demo-${each.key}"
  output_path = "${local.build}/zip/demo-${each.key}.zip"
}

resource "aws_cloudwatch_log_group" "demo" {
  for_each          = local.demo_functions
  name              = "/aws/lambda/${each.value.name}"
  retention_in_days = local.log_days
  tags              = local.managed
}

resource "aws_lambda_function" "demo" {
  for_each         = local.demo_functions
  function_name    = each.value.name
  role             = aws_iam_role.demo[each.key].arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.demo[each.key].output_path
  source_code_hash = data.archive_file.demo[each.key].output_base64sha256
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  publish          = each.key == "api"
  environment {
    variables = each.key == "traffic" ? { API_URL = aws_apigatewayv2_api.orders.api_endpoint } : {
      TABLE_NAME = aws_dynamodb_table.orders.name
      QUEUE_URL  = aws_sqs_queue.orders.url
    }
  }
  tags       = local.managed
  depends_on = [aws_cloudwatch_log_group.demo, aws_iam_role_policy_attachment.demo_logs]
}

# Traffic goes to the alias, so a deploy is "point live at a new version"
# and a rollback is "point it back".
resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.demo["api"].function_name
  function_version = aws_lambda_function.demo["api"].version
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.orders.arn
  function_name    = aws_lambda_function.demo["worker"].arn
  batch_size       = 1
  tags             = local.managed
}

# --- HTTP API ---

resource "aws_apigatewayv2_api" "orders" {
  name          = "demo-orders"
  protocol_type = "HTTP"
  tags          = local.managed
}

resource "aws_apigatewayv2_integration" "orders" {
  api_id                 = aws_apigatewayv2_api.orders.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_alias.live.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "orders" {
  for_each  = toset(["POST /orders", "GET /health"])
  api_id    = aws_apigatewayv2_api.orders.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.orders.id}"
}

resource "aws_apigatewayv2_stage" "orders" {
  api_id      = aws_apigatewayv2_api.orders.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
  tags = local.managed
}

resource "aws_lambda_permission" "api" {
  statement_id  = "apigw"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.demo["api"].function_name
  qualifier     = aws_lambda_alias.live.name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.orders.execution_arn}/*/*"
}

# --- traffic ---

resource "aws_cloudwatch_event_rule" "traffic" {
  name                = "demo-traffic-schedule"
  description         = "Places a few demo orders every minute"
  schedule_expression = "rate(1 minute)"
  tags                = local.managed
}

resource "aws_cloudwatch_event_target" "traffic" {
  rule = aws_cloudwatch_event_rule.traffic.name
  arn  = aws_lambda_function.demo["traffic"].arn
}

resource "aws_lambda_permission" "traffic" {
  statement_id  = "events"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.demo["traffic"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.traffic.arn
}

# --- alarms: every one whose name starts with "demo-" opens an incident ---

locals {
  alarms = {
    "demo-api-5xx" = {
      namespace = "AWS/ApiGateway", metric = "5xx", dims = { ApiId = aws_apigatewayv2_api.orders.id },
      stat      = "Sum", op = "GreaterThanOrEqualToThreshold", threshold = 1, periods = 2, missing = "notBreaching",
      desc      = "The orders API returns server errors"
    }
    "demo-api-errors" = {
      namespace = "AWS/Lambda", metric = "Errors", dims = { FunctionName = "demo-orders-api" },
      stat      = "Sum", op = "GreaterThanOrEqualToThreshold", threshold = 1, periods = 2, missing = "notBreaching",
      desc      = "demo-orders-api invocations fail"
    }
    "demo-api-throttles" = {
      namespace = "AWS/Lambda", metric = "Throttles", dims = { FunctionName = "demo-orders-api" },
      stat      = "Sum", op = "GreaterThanOrEqualToThreshold", threshold = 1, periods = 2, missing = "notBreaching",
      desc      = "demo-orders-api invocations are throttled"
    }
    "demo-worker-errors" = {
      namespace = "AWS/Lambda", metric = "Errors", dims = { FunctionName = "demo-orders-worker" },
      stat      = "Sum", op = "GreaterThanOrEqualToThreshold", threshold = 1, periods = 2, missing = "notBreaching",
      desc      = "demo-orders-worker fails to process orders"
    }
    "demo-orders-backlog" = {
      namespace = "AWS/SQS", metric = "ApproximateAgeOfOldestMessage", dims = { QueueName = "demo-orders" },
      stat      = "Maximum", op = "GreaterThanThreshold", threshold = 180, periods = 2, missing = "notBreaching",
      desc      = "Orders wait in the queue for more than 3 minutes"
    }
    "demo-traffic-stopped" = {
      namespace = "AWS/Lambda", metric = "Invocations", dims = { FunctionName = "demo-traffic" },
      stat      = "Sum", op = "LessThanThreshold", threshold = 1, periods = 3, missing = "breaching",
      desc      = "No demo traffic has run for 3 minutes"
    }
  }
}

# A brand-new "no traffic" alarm has no data yet and would fire at once, so it
# is created only after traffic has run for a few minutes.
resource "time_sleep" "traffic_warmup" {
  depends_on      = [aws_cloudwatch_event_target.traffic, aws_lambda_permission.traffic]
  create_duration = "4m"
}

resource "aws_cloudwatch_metric_alarm" "demo" {
  for_each            = local.alarms
  alarm_name          = each.key
  alarm_description   = each.value.desc
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dims
  statistic           = each.value.stat
  period              = 60
  evaluation_periods  = each.value.periods
  datapoints_to_alarm = each.value.periods
  comparison_operator = each.value.op
  threshold           = each.value.threshold
  treat_missing_data  = each.value.missing
  tags                = local.managed
  depends_on          = [time_sleep.traffic_warmup]
}
