#!/bin/bash

# Configuration
PROJECT_ID=$(gcloud config get-value project)
SERVICE_NAME="smart-email-manager-agent"
REGION="us-central1"
REPO_NAME="agent-repo"
IMAGE_NAME="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$SERVICE_NAME"

echo "🚀 Starting deployment for $SERVICE_NAME..."

# 1. Create Artifact Registry repository if it doesn't exist
echo "🛠️ Ensuring Artifact Registry repository exists..."
gcloud artifacts repositories create $REPO_NAME \
  --repository-format=docker \
  --location=$REGION \
  --description="Docker repository for Rapid Agent Gmail Suite" \
  --project $PROJECT_ID \
  2>/dev/null || true

# 2. Build the image using Cloud Build
echo "📦 Building container image..."
gcloud builds submit --tag $IMAGE_NAME . --project $PROJECT_ID

# 3. Deploy to Cloud Run
echo "🚢 Deploying to Cloud Run..."
gcloud run deploy $SERVICE_NAME \
  --image $IMAGE_NAME \
  --platform managed \
  --region $REGION \
  --allow-unauthenticated \
  --port 8080 \
  --set-env-vars="PROJECT_ID=$PROJECT_ID" \
  --project $PROJECT_ID

echo "✅ Deployment complete!"
echo "🔗 Service URL: $(gcloud run services describe $SERVICE_NAME --platform managed --region $REGION --format 'value(status.url)')"
