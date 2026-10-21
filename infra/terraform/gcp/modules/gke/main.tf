resource "google_container_cluster" "platform" {
  count = var.enabled ? 1 : 0

  project                  = var.project_id
  name                     = var.cluster_name
  location                 = var.zone
  network                  = var.network_self_link
  subnetwork               = var.subnet_self_link
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.deletion_protection
  enable_shielded_nodes    = true

  release_channel {
    channel = "REGULAR"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = toset(var.master_authorized_cidrs)
      content {
        cidr_block = cidr_blocks.value
      }
    }
  }

  ip_allocation_policy {
    stack_type = "IPV4"
  }

  resource_labels = {
    environment = "dev"
    project     = "ewallet"
    managed_by  = "terraform"
  }

  lifecycle {
    precondition {
      condition     = length(var.master_authorized_cidrs) > 0
      error_message = "GKE requires at least one explicit administrator CIDR; 0.0.0.0/0 is not accepted by repository policy."
    }
    precondition {
      condition     = !contains(var.master_authorized_cidrs, "0.0.0.0/0")
      error_message = "GKE control-plane access cannot be open to 0.0.0.0/0."
    }
  }
}

resource "google_container_node_pool" "cpu" {
  count = var.enabled ? 1 : 0

  project            = var.project_id
  name               = "cpu-pool"
  location           = var.zone
  cluster            = google_container_cluster.platform[0].name
  initial_node_count = var.initial_node_count

  autoscaling {
    min_node_count = var.min_node_count
    max_node_count = var.max_node_count
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = var.disk_size_gb
    image_type      = "COS_CONTAINERD"
    service_account = var.node_service_account_email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    labels = {
      workload = "cpu"
    }
  }
}
