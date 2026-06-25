output "namespace_app" {
  value = kubernetes_namespace.app.metadata[0].name
}

output "namespace_data" {
  value = kubernetes_namespace.data.metadata[0].name
}

output "namespace_monitoring" {
  value = kubernetes_namespace.monitoring.metadata[0].name
}

output "namespace_pipeline" {
  value = kubernetes_namespace.pipeline.metadata[0].name
}
