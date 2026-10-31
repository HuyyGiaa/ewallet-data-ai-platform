output "data_bucket_name" {
  description = "Canonical platform data bucket."
  value       = google_storage_bucket.canonical.name
}

output "data_bucket_url" {
  description = "Canonical bucket URI used by runtime profiles."
  value       = google_storage_bucket.canonical.url
}

output "temporary_bucket_name" {
  description = "Regenerable staging bucket."
  value       = google_storage_bucket.temporary.name
}

output "temporary_bucket_url" {
  description = "Regenerable staging bucket URI."
  value       = google_storage_bucket.temporary.url
}
