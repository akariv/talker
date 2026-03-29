# Cloud Run SA: read Firestore
resource "google_project_iam_member" "run_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.cloud_run.email}"
}

# Cloud Run SA: read secrets
resource "google_project_iam_member" "run_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.cloud_run.email}"
}

# GitHub Actions SA: push Docker images
resource "google_project_iam_member" "gh_artifact_registry" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.gh_actions.email}"
}

# GitHub Actions SA: deploy Cloud Run
resource "google_project_iam_member" "gh_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.gh_actions.email}"
}

# GitHub Actions SA: act as Cloud Run SA during deploy
resource "google_project_iam_member" "gh_sa_user" {
  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = "serviceAccount:${google_service_account.gh_actions.email}"
}
