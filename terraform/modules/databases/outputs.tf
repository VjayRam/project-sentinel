output "postgres_host" {
  description = "PostgreSQL ClusterIP service name (reachable within the cluster)."
  value       = "postgresql.${var.namespace}.svc.cluster.local"
}

output "postgres_db" {
  value = "sentinel"
}

output "postgres_user" {
  value = "sentinel"
}

output "mongodb_host" {
  description = "MongoDB ClusterIP service name (reachable within the cluster)."
  value       = "mongodb.${var.namespace}.svc.cluster.local"
}
