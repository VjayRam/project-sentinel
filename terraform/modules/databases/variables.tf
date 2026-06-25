variable "namespace" {
  description = "Namespace to deploy databases into."
  type        = string
}

variable "postgres_password" {
  description = "Password for the sentinel PostgreSQL user."
  type        = string
  sensitive   = true
  default     = "sentinel"
}

variable "mongodb_password" {
  description = "Password for the sentinel MongoDB user."
  type        = string
  sensitive   = true
  default     = "sentinel"
}
