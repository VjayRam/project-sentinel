variable "k8s_context" {
  description = "kubectl context name created by k3d"
  type        = string
  default     = "k3d-sentinel"
}

variable "postgres_password" {
  description = "Password for the sentinel PostgreSQL user"
  type        = string
  sensitive   = true
  default     = "sentinel"
}

variable "postgres_storage_size" {
  description = "PVC size for PostgreSQL data"
  type        = string
  default     = "2Gi"
}

variable "mongodb_root_password" {
  description = "Password for the MongoDB root user"
  type        = string
  sensitive   = true
  default     = "sentinel-root"
}

variable "mongodb_password" {
  description = "Password for the sentinel MongoDB user"
  type        = string
  sensitive   = true
  default     = "sentinel"
}

variable "mongodb_storage_size" {
  description = "PVC size for MongoDB data"
  type        = string
  default     = "2Gi"
}
