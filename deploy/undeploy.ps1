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

# 4. Delete Vertex AI (Agent Builder) Resources
Write-Host "🗑️ Cleaning up Vertex AI resources..." -ForegroundColor Yellow
$token = gcloud auth print-access-token

$headers = @{
    "Authorization" = "Bearer $token"
    "Content-Type"  = "application/json"
    "x-goog-user-project" = $projectId
}

# Delete Engine
$enginesUrl = "https://discoveryengine.googleapis.com/v1beta/projects/$projectId/locations/global/collections/default_collection/engines"
try {
    $enginesResponse = Invoke-RestMethod -Uri $enginesUrl -Method Get -Headers $headers
    $engine = $enginesResponse.engines | Where-Object { $_.displayName -eq "Smart Email Manager" }
    
    if ($engine) {
        $engineId = ($engine.name -split '/')[-1]
        Write-Host "Deleting Engine: $engineId" -ForegroundColor Yellow
        $deleteUrl = "https://discoveryengine.googleapis.com/v1beta/projects/$projectId/locations/global/collections/default_collection/engines/$engineId"
        Invoke-RestMethod -Uri $deleteUrl -Method Delete -Headers $headers | Out-Null
    } else {
        Write-Host "Engine 'Smart Email Manager' not found." -ForegroundColor White
    }
} catch {
    Write-Host "Error fetching engines: $($_.Exception.Message)" -ForegroundColor Red
}

# Delete Data Store
$dsUrl = "https://discoveryengine.googleapis.com/v1beta/projects/$projectId/locations/global/collections/default_collection/dataStores"
try {
    $dsResponse = Invoke-RestMethod -Uri $dsUrl -Method Get -Headers $headers
    $ds = $dsResponse.dataStores | Where-Object { $_.displayName -eq "Email Knowledge Base" }
    
    if ($ds) {
        $dsId = ($ds.name -split '/')[-1]
        Write-Host "Deleting Data Store: $dsId" -ForegroundColor Yellow
        $deleteDsUrl = "https://discoveryengine.googleapis.com/v1beta/projects/$projectId/locations/global/collections/default_collection/dataStores/$dsId"
        Invoke-RestMethod -Uri $deleteDsUrl -Method Delete -Headers $headers | Out-Null
    } else {
        Write-Host "Data Store 'Email Knowledge Base' not found." -ForegroundColor White
    }
} catch {
    Write-Host "Error fetching data stores: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "✅ Cleanup tasks completed." -ForegroundColor Green
