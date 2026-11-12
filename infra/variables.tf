variable "region" {
  type    = string
  default = "eu-north-1"
}

variable "email" {
  description = "Where approval requests and incident results go. Confirm the SNS subscription email once."
  type        = string
}

variable "openrouter_api_key" {
  description = "Stored as an SSM SecureString. It also ends up in the local Terraform state, which is gitignored."
  type        = string
  sensitive   = true
}

variable "model" {
  description = "Any OpenRouter model with tool calling."
  type        = string
  default     = "anthropic/claude-haiku-5.5"
}

variable "price_in_per_m" {
  description = "Fallback USD per million input tokens, used only if OpenRouter does not report the cost."
  type        = number
  default     = 0.10
}

variable "price_out_per_m" {
  type    = number
  default = 0.50
}

variable "max_attempts" {
  description = "Proposals per incident before it is escalated to you."
  type        = number
  default     = 3
}

variable "max_tool_calls" {
  type    = number
  default = 25
}

variable "max_cost_usd" {
  description = "Model spend cap per investigation."
  type        = number
  default     = 0.05
}

variable "approval_timeout_s" {
  type    = number
  default = 3600
}

variable "verify_wait_s" {
  description = "Seconds between alarm checks after a fix."
  type        = number
  default     = 60
}

variable "verify_polls" {
  description = "Extra checks before the fix is undone. Total wait is about (polls + 1) * verify_wait_s."
  type        = number
  default     = 8
}
