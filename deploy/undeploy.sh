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

# 4. Delete Vertex AI (Agent Builder) Resources
echo "🗑️ Cleaning up Vertex AI resources..."
TOKEN=$(gcloud auth print-access-token)

# Delete Engine
ENGINE_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
    -H "x-goog-user-project: $PROJECT_ID" \
    "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/engines" \
    | grep -B 1 '"displayName": "Smart Email Manager"' | grep '"name":' | sed -E 's/.*\/engines\/([^"]+)".*/\1/')

if [ ! -z "$ENGINE_ID" ]; then
    echo "Deleting Engine: $ENGINE_ID"
    curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
        -H "x-goog-user-project: $PROJECT_ID" \
        "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/engines/$ENGINE_ID" > /dev/null
else
    echo "Engine 'Smart Email Manager' not found."
fi

# Delete Data Store
DS_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
    -H "x-goog-user-project: $PROJECT_ID" \
    "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/dataStores" \
    | grep -B 1 '"displayName": "Email Knowledge Base"' | grep '"name":' | sed -E 's/.*\/dataStores\/([^"]+)".*/\1/')

if [ ! -z "$DS_ID" ]; then
    echo "Deleting Data Store: $DS_ID"
    curl -s -X DELETE -H "Authorization: Bearer $TOKEN" \
        -H "x-goog-user-project: $PROJECT_ID" \
        "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/dataStores/$DS_ID" > /dev/null
else
    echo "Data Store 'Email Knowledge Base' not found."
fi

echo "✅ Cleanup tasks completed."
