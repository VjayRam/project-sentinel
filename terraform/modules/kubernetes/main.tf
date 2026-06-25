resource "kubernetes_namespace" "app" {
  metadata {
    name   = "sentinel-app"
    labels = var.labels
  }
}

resource "kubernetes_namespace" "data" {
  metadata {
    name   = "sentinel-data"
    labels = var.labels
  }
}

resource "kubernetes_namespace" "monitoring" {
  metadata {
    name   = "sentinel-monitoring"
    labels = var.labels
  }
}

resource "kubernetes_namespace" "pipeline" {
  metadata {
    name   = "sentinel-pipeline"
    labels = var.labels
  }
}
