output "binding_ids" {
  description = "Workload Identity binding IDs, empty while GKE is disabled."
  value = {
    for key, binding in google_service_account_iam_member.workload_identity :
    key => binding.id
  }
}
