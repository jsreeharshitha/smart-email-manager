# Configuration
$ProjectId = $(gcloud config get-value project)
$ServiceName = "smart-email-manager-agent"
$Region = "us-central1"
$ImageName = "gcr.io/$ProjectId/$ServiceName"

Write-Host "🚀 Starting deployment for $ServiceName..." -ForegroundColor Cyan

# 1. Build the image using Cloud Build
Write-Host "📦 Building container image..." -ForegroundColor Yellow
gcloud builds submit --tag $ImageName . --project $ProjectId

# 2. Deploy to Cloud Run
Write-Host "🚢 Deploying to Cloud Run..." -ForegroundColor Yellow
gcloud run deploy $ServiceName `
  --image $ImageName `
  --platform managed `
  --region $Region `
  --allow-unauthenticated `
  --port 8080 `
  --set-env-vars="PROJECT_ID=$ProjectId" `
  --project $ProjectId

$ServiceUrl = $(gcloud run services describe $ServiceName --platform managed --region $Region --format 'value(status.url)')
Write-Host "✅ Deployment complete!" -ForegroundColor Green
Write-Host "🔗 Service URL: $ServiceUrl" -ForegroundColor Cyan
