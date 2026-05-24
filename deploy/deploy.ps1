# Configuration
$ProjectId = $(gcloud config get-value project)
$ServiceName = "smart-email-manager-agent"
$Region = "us-central1"
$RepoName = "agent-repo"
$ImageName = "$Region-docker.pkg.dev/$ProjectId/$RepoName/$ServiceName"

Write-Host "🚀 Starting deployment for $ServiceName..." -ForegroundColor Cyan

# 1. Create Artifact Registry repository if it doesn't exist
Write-Host "🛠️ Ensuring Artifact Registry repository exists..." -ForegroundColor Yellow
gcloud artifacts repositories create $RepoName `
  --repository-format=docker `
  --location=$Region `
  --description="Docker repository for Rapid Agent Gmail Suite" `
  --project $ProjectId `
  2>$null # Ignore error if it already exists

# 2. Build the image using Cloud Build
Write-Host "📦 Building container image..." -ForegroundColor Yellow
gcloud builds submit --tag $ImageName . --project $ProjectId

# 3. Deploy to Cloud Run
Write-Host "🚢 Deploying to Cloud Run..." -ForegroundColor Yellow
gcloud run deploy $ServiceName `
  --image $ImageName `
  --platform managed `
  --region $Region `
  --allow-unauthenticated `
  --port 8080 `
  --set-env-vars="PROJECT_ID=$ProjectId,MONGO_URI=mongodb+srv://rahulgputcha_db_user:kRNlVZ9FmHovmAHi@personalemailmanager-md.p9mjsu8.mongodb.net/?appName=PersonalEmailManager-MDBCluster,VOYAGE_API_KEY=pa-V-Bk90sYKKs3sNbb1DUzn7Z2DID7ZttqLlzsSQW_tSk" `
  --project $ProjectId

$ServiceUrl = $(gcloud run services describe $ServiceName --platform managed --region $Region --format 'value(status.url)')
Write-Host "✅ Deployment complete!" -ForegroundColor Green
Write-Host "🔗 Service URL: $ServiceUrl" -ForegroundColor Cyan
