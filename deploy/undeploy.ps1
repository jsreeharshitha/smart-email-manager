# Smart Email Manager - Cleanup Script (PowerShell)
# This script removes the Cloud Run service, Pub/Sub resources, and container images.

$projectId = gcloud config get-value project
$region = gcloud config get-value run/region
if ([string]::IsNullOrEmpty($region)) { $region = "us-central1" }

Write-Host "Starting infrastructure cleanup for project: $projectId in region: $region" -ForegroundColor Cyan

# 1. Delete Cloud Run Service
Write-Host "🗑️ Deleting Cloud Run service: smart-email-manager-agent..." -ForegroundColor Yellow
gcloud run services delete smart-email-manager-agent --region $region --quiet

# 2. Delete Pub/Sub Resources
Write-Host "🗑️ Deleting Pub/Sub resources..." -ForegroundColor Yellow
gcloud pubsub subscriptions delete gmail-notifications-sub --quiet 2>$null
gcloud pubsub topics delete gmail-notifications --quiet 2>$null

# 3. Delete Artifacts
Write-Host "🗑️ Deleting container images from GCR..." -ForegroundColor Yellow
gcloud container images delete "gcr.io/$projectId/smart-email-manager-agent" --force-delete-tags --quiet 2>$null

# 4. Vertex AI (Agent Builder) Instructions
Write-Host "------------------------------------------------" -ForegroundColor White
Write-Host "⚠️  Vertex AI (Agent Builder) Note:" -ForegroundColor Red
Write-Host "Because Data Store and Engine IDs are generated dynamically,"
Write-Host "it is recommended to delete them via the GCP Console:"
Write-Host "1. Go to 'Agent Builder > Apps' and delete 'Smart Email Manager'."
Write-Host "2. Go to 'Agent Builder > Data Stores' and delete 'Email Knowledge Base'."
Write-Host "------------------------------------------------" -ForegroundColor White

Write-Host "✅ Cleanup tasks completed." -ForegroundColor Green
