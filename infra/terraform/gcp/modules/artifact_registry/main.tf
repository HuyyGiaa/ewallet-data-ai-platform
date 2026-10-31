resource "google_artifact_registry_repository" "docker" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  description   = "Immutable E-Wallet platform images tagged by Git SHA"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }
}
