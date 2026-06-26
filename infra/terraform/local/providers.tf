terraform {
  required_version = ">= 1.6"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.27"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
  }
}

# Both providers read from the same kubeconfig context that k3d creates.
# k3d cluster create sentinel sets the context name to "k3d-sentinel".
provider "kubernetes" {
  config_path    = "~/.kube/config"
  config_context = var.k8s_context
}

provider "helm" {
  kubernetes {
    config_path    = "~/.kube/config"
    config_context = var.k8s_context
  }
}
