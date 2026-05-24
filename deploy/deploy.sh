#!/bin/bash

# Configuration
PROJECT_ID=$(gcloud config get-value project)
SERVICE_NAME="smart-email-manager-agent"
REGION="us-central1"
IMAGE_NAME="gcr.io/$PROJECT_ID/$SERVICE_NAME"

echo "🚀 Starting deployment for $SERVICE_NAME..."

# 1. Build the image using Cloud Build
echo "📦 Building container image..."
gcloud builds submit --tag $IMAGE_NAME . --project $PROJECT_ID

# 2. Deploy to Cloud Run
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
