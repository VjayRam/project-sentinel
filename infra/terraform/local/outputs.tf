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

output "mongo_express_port_forward" {
  description = "Command to open mongo-express in your browser (http://localhost:8081)"
  value       = "kubectl port-forward -n sentinel-data svc/mongo-express 8081:8081"
}

output "kafka_port_forward" {
  description = "Command to reach Kafka (EXTERNAL listener) from your local machine"
  value       = "kubectl port-forward -n sentinel-data svc/kafka 9094:9094"
}

output "jaeger_port_forward" {
  description = "Command to open Jaeger UI from your local machine (http://localhost:16686)"
  value       = "kubectl port-forward -n sentinel-monitoring svc/jaeger 16686:16686"
}

output "otel_collector_grpc_port_forward" {
  description = "Command to send OTLP gRPC traces to the collector from your local machine (:4317)"
  value       = "kubectl port-forward -n sentinel-monitoring svc/otel-collector 4317:4317"
}

output "otel_collector_http_port_forward" {
  description = "Command to send OTLP HTTP traces to the collector from your local machine (:4318)"
  value       = "kubectl port-forward -n sentinel-monitoring svc/otel-collector 4318:4318"
}

output "airflow_webserver_port_forward" {
  description = "Command to open the Airflow UI from your local machine (http://localhost:8090, admin/<airflow_admin_password>). Local port 8090, not 8080 — k3d's serverlb container already publishes host port 8080 for its own ingress."
  value       = "kubectl port-forward -n sentinel-pipeline svc/airflow-webserver 8090:8080"
}

output "mlflow_port_forward" {
  description = "Command to open the MLflow UI from your local machine (http://localhost:5000)"
  value       = "kubectl port-forward -n sentinel-monitoring svc/mlflow 5000:5000"
}
