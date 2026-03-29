resource "google_artifact_registry_repository" "docker" {
  location      = var.region
  repository_id = "talker"
  format        = "DOCKER"
  description   = "Docker images for Talker"

  depends_on = [google_project_service.apis]
}
