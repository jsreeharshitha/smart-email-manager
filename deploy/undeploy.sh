#!/bin/bash

# Smart Email Manager - Cleanup Script
# This script removes the Cloud Run service, Pub/Sub resources, and container images.

PROJECT_ID=$(gcloud config get-value project)
REGION=$(gcloud config get-value run/region)
REGION=${REGION:-"us-central1"}

echo "🚀 Starting infrastructure cleanup for project: $PROJECT_ID in region: $REGION"

# 1. Delete Cloud Run Service
echo "🗑️ Deleting Cloud Run service: smart-email-manager-agent..."
gcloud run services delete smart-email-manager-agent --region "$REGION" --quiet

# 2. Delete Pub/Sub Resources
echo "🗑️ Deleting Pub/Sub subscription: gmail-notifications-sub..."
gcloud pubsub subscriptions delete gmail-notifications-sub --quiet 2>/dev/null

echo "🗑️ Deleting Pub/Sub topic: gmail-notifications..."
gcloud pubsub topics delete gmail-notifications --quiet 2>/dev/null

# 3. Delete Artifacts
echo "🗑️ Deleting container images from GCR..."
gcloud container images delete "gcr.io/$PROJECT_ID/smart-email-manager-agent" --force-delete-tags --quiet 2>/dev/null

# 4. Vertex AI (Agent Builder) Instructions
echo "------------------------------------------------"
echo "⚠️  Vertex AI (Agent Builder) Note:"
echo "Because Data Store and Engine IDs are generated dynamically,"
echo "it is recommended to delete them via the GCP Console:"
echo "1. Go to 'Agent Builder > Apps' and delete 'Smart Email Manager'."
echo "2. Go to 'Agent Builder > Data Stores' and delete 'Email Knowledge Base'."
echo "------------------------------------------------"

echo "✅ Cleanup tasks completed."
