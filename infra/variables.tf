variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "repo_name" {
  description = "GitHub repository (owner/repo)"
  type        = string
  default     = "akariv/talker"
}

variable "cloud_run_service_name" {
  description = "Cloud Run service name"
  type        = string
  default     = "talker-server"
}
