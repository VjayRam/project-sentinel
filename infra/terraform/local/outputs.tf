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
