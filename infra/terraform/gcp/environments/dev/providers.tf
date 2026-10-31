provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  default_labels = {
    environment = "dev"
    project     = "ewallet"
    managed_by  = "terraform"
  }
}
