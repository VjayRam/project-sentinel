variable "labels" {
  description = "Labels applied to every namespace."
  type        = map(string)
  default     = { "managed-by" = "terraform", "project" = "sentinel" }
}
