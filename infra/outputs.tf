output "cloud_run_url" {
  description = "Cloud Run service URL"
  value       = google_cloud_run_v2_service.server.uri
}

output "workload_identity_provider" {
  description = "Full WIF provider path (for GitHub Actions)"
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "gh_actions_service_account_email" {
  description = "GitHub Actions SA email (for GitHub Actions)"
  value       = google_service_account.gh_actions.email
}

output "docker_image" {
  description = "Docker image URI (without tag)"
  value       = local.image
}
