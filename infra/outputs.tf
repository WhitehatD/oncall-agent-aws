output "region" {
  value = var.region
}

output "orders_api_url" {
  value = aws_apigatewayv2_api.orders.api_endpoint
}

output "approval_url" {
  value = aws_apigatewayv2_api.approval.api_endpoint
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.incident.arn
}

output "incidents_table" {
  value = aws_dynamodb_table.incidents.name
}

output "orders_queue_url" {
  value = aws_sqs_queue.orders.url
}

output "worker_mapping_uuid" {
  value = aws_lambda_event_source_mapping.worker.uuid
}

output "worker_policy" {
  description = "The worker's correct inline policy, used by chaos.py reset"
  value       = aws_iam_role_policy.worker.policy
}
