data "google_compute_image" "ubuntu" {
  family  = "ubuntu-2404-lts-amd64"
  project = "ubuntu-os-cloud"
}

resource "google_compute_firewall" "iap_ssh" {
  count = var.enabled ? 1 : 0

  project       = var.project_id
  name          = "${var.instance_name}-allow-iap-ssh"
  network       = var.network_self_link
  direction     = "INGRESS"
  source_ranges = var.ssh_source_ranges
  target_tags   = ["jenkins"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_instance" "jenkins" {
  count = var.enabled ? 1 : 0

  project             = var.project_id
  name                = var.instance_name
  zone                = var.zone
  machine_type        = var.machine_type
  deletion_protection = var.deletion_protection
  tags                = ["jenkins"]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      size  = var.boot_disk_size_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    network    = var.network_self_link
    subnetwork = var.subnet_self_link

    # Ephemeral egress IP for package installation. Ingress remains IAP-only.
    access_config {}
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
  }

  service_account {
    email  = var.service_account_email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  labels = {
    environment = "dev"
    project     = "ewallet"
    managed_by  = "terraform"
  }
}
