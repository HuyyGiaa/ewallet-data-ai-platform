locals {
  data_bucket_name      = coalesce(var.data_bucket_name, "${var.project_id}-data")
  temporary_bucket_name = coalesce(var.temporary_bucket_name, "${var.project_id}-temp")

  required_services = toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "dataproc.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "iap.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])
}

module "project_services" {
  source = "../../modules/project_services"

  project_id = var.project_id
  services   = local.required_services
}

module "network" {
  source = "../../modules/network"

  project_id   = var.project_id
  region       = var.region
  network_name = "ewallet-dev-vpc"
  subnet_name  = "ewallet-dev-${var.region}"
  subnet_cidr  = var.subnet_cidr

  depends_on = [module.project_services]
}

module "storage" {
  source = "../../modules/storage"

  project_id            = var.project_id
  region                = var.region
  data_bucket_name      = local.data_bucket_name
  temporary_bucket_name = local.temporary_bucket_name

  depends_on = [module.project_services]
}

module "artifact_registry" {
  source = "../../modules/artifact_registry"

  project_id    = var.project_id
  region        = var.region
  repository_id = "ewallet-platform"

  depends_on = [module.project_services]
}

module "iam" {
  source = "../../modules/iam"

  project_id                 = var.project_id
  data_bucket_name           = module.storage.data_bucket_name
  temporary_bucket_name      = module.storage.temporary_bucket_name
  dataproc_submitter_members = var.dataproc_submitter_members

  depends_on = [module.project_services, module.storage, module.artifact_registry]
}

module "dataproc" {
  source = "../../modules/dataproc"

  project_id            = var.project_id
  region                = var.region
  subnet_self_link      = module.network.subnet_self_link
  service_account_email = module.iam.spark_service_account_email
  temporary_bucket_name = module.storage.temporary_bucket_name

  depends_on = [module.project_services, module.network, module.iam]
}

module "gke" {
  source = "../../modules/gke"

  enabled                    = var.enable_gke
  deletion_protection        = var.gke_deletion_protection
  project_id                 = var.project_id
  zone                       = var.zone
  cluster_name               = "ewallet-dev"
  network_self_link          = module.network.network_self_link
  subnet_self_link           = module.network.subnet_self_link
  node_service_account_email = module.iam.gke_node_service_account_email
  master_authorized_cidrs    = var.gke_master_authorized_cidrs
  machine_type               = var.gke_machine_type
  min_node_count             = var.gke_min_nodes
  max_node_count             = var.gke_max_nodes

  depends_on = [module.project_services, module.network, module.iam]
}

module "jenkins_vm" {
  source = "../../modules/jenkins_vm"

  enabled               = var.enable_jenkins_vm
  deletion_protection   = var.jenkins_deletion_protection
  project_id            = var.project_id
  zone                  = var.zone
  instance_name         = "ewallet-jenkins"
  machine_type          = var.jenkins_machine_type
  network_self_link     = module.network.network_self_link
  subnet_self_link      = module.network.subnet_self_link
  service_account_email = module.iam.ci_service_account_email

  depends_on = [module.project_services, module.network, module.iam]
}
