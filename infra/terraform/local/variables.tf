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

variable "minio_root_user" {
  description = "MinIO root username (S3 access key ID)"
  type        = string
  default     = "sentinel"
}

variable "minio_root_password" {
  description = "MinIO root password (S3 secret access key, min 8 chars)"
  type        = string
  sensitive   = true
  default     = "sentinel-minio"
}

variable "minio_storage_size" {
  description = "PVC size for MinIO data"
  type        = string
  default     = "5Gi"
}

variable "prometheus_storage_size" {
  description = "PVC size for Prometheus TSDB (7-day retention)"
  type        = string
  default     = "5Gi"
}

variable "grafana_admin_password" {
  description = "Grafana admin user password"
  type        = string
  sensitive   = true
  default     = "admin"
}

variable "kafka_storage_size" {
  description = "PVC size for Kafka data"
  type        = string
  default     = "2Gi"
}
