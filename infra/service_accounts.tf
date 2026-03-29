resource "google_service_account" "cloud_run" {
  account_id   = "talker-run"
  display_name = "Talker Cloud Run"
  description  = "Service account for the Talker Cloud Run service"
}

resource "google_service_account" "gh_actions" {
  account_id   = "talker-gh-actions"
  display_name = "Talker GitHub Actions"
  description  = "Service account for GitHub Actions CI/CD"
}
