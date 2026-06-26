output "postgres_host" {
  description = "PostgreSQL service DNS name inside the cluster"
  value       = "postgresql.sentinel-data.svc.cluster.local"
}

output "postgres_port" {
  value = 5432
}

output "postgres_database" {
  value = "sentinel"
}

output "postgres_username" {
  value = "sentinel"
}

output "postgres_connection_string" {
  description = "DSN for services connecting to PostgreSQL from inside the cluster"
  value       = "postgresql://sentinel:${var.postgres_password}@postgresql.sentinel-data.svc.cluster.local:5432/sentinel"
  sensitive   = true
}

output "postgres_kubectl_port_forward" {
  description = "Command to reach PostgreSQL from your local machine"
  value       = "kubectl port-forward -n sentinel-data svc/postgresql 5432:5432"
}

output "mongodb_host" {
  description = "MongoDB service DNS name inside the cluster"
  value       = "mongodb.sentinel-data.svc.cluster.local"
}

output "mongodb_port" {
  value = 27017
}

output "mongodb_connection_string" {
  description = "URI for services connecting to MongoDB from inside the cluster"
  value       = "mongodb://sentinel:${var.mongodb_password}@mongodb.sentinel-data.svc.cluster.local:27017/sentinel"
  sensitive   = true
}

output "mongodb_kubectl_port_forward" {
  description = "Command to reach MongoDB from your local machine"
  value       = "kubectl port-forward -n sentinel-data svc/mongodb 27017:27017"
}

output "minio_endpoint" {
  description = "MinIO S3 API endpoint inside the cluster (used by application code)"
  value       = "http://minio.sentinel-data.svc.cluster.local:9000"
}

output "minio_access_key" {
  description = "MinIO access key ID (MINIO_ROOT_USER)"
  value       = var.minio_root_user
}

output "minio_secret_key" {
  description = "MinIO secret access key (MINIO_ROOT_PASSWORD)"
  value       = var.minio_root_password
  sensitive   = true
}

output "minio_api_port_forward" {
  description = "Command to reach the MinIO S3 API from your local machine"
  value       = "kubectl port-forward -n sentinel-data svc/minio 9000:9000"
}

output "minio_console_port_forward" {
  description = "Command to open the MinIO web console in your browser (http://localhost:9001)"
  value       = "kubectl port-forward -n sentinel-data svc/minio 9001:9001"
}

output "prometheus_port_forward" {
  description = "Command to reach the Prometheus UI from your local machine (http://localhost:9090)"
  value       = "kubectl port-forward -n sentinel-monitoring svc/prometheus 9090:9090"
}

output "grafana_port_forward" {
  description = "Command to open Grafana in your browser (http://localhost:3000, admin/admin)"
  value       = "kubectl port-forward -n sentinel-monitoring svc/grafana 3000:3000"
}
