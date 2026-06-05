#!/bin/bash

# Master Deployment Script: Smart Email Manager Stack
# --------------------------------------------------
PROJECT_ID=$(gcloud config get-value project)
REGION="${CHOSEN_REGION:-us-central1}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/agent-repo/smart-email-manager-agent:latest"

# 0. Load Secrets from .env (located in submodule root)
ENV_FILE="../.env"
if [ -f "$ENV_FILE" ]; then
    echo "[*] Loading environment variables from .env..."
    # Robust loading: strip quotes and handle comments
    export $(grep -v '^#' "$ENV_FILE" | sed 's/[\"'\'']//g' | xargs)
fi

# 1. Provision Vertex AI Infrastructure
TOKEN=$(gcloud auth print-access-token)

echo -e "\n[1/3] Provisioning Vertex AI Data Store..."
curl -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H "X-Goog-User-Project: $PROJECT_ID" \
      -H "Content-Type: application/json" \
      -d "@datastore_payload.json" \
      "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/dataStores?dataStoreId=smart-email-manager-ds"

echo -e "\n[2/3] Provisioning Vertex AI Search Engine..."
curl -X POST \
      -H "Authorization: Bearer $TOKEN" \
      -H "X-Goog-User-Project: $PROJECT_ID" \
      -H "Content-Type: application/json" \
      -d "@engine_payload.json" \
      "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/engines?engineId=smart-email-manager"

# 2. Build and Deploy SEM Agent
echo -e "\n[3/4] Building and Pushing SEM Agent Image..."
gcloud builds submit --tag $IMAGE ..

echo -e "\n[4/4] Deploying SEM Agent to Cloud Run..."
gcloud run deploy smart-email-manager-agent \
    --image $IMAGE \
    --platform managed --region $REGION --allow-unauthenticated \
    --memory 4Gi --cpu 1 --concurrency 10 \
    --set-env-vars="MONGO_URI=$MONGO_URI,VOYAGE_API_KEY=$VOYAGE_API_KEY,PROJECT_ID=$PROJECT_ID"

# 3. Post-Deployment: Set CLOUD_RUN_URL
SERVICE_INFO=$(gcloud run services describe smart-email-manager-agent --platform managed --region $REGION)
SERVICE_URL=$(echo "$SERVICE_INFO" | grep -oP 'URL:\s+\K(https?://\S+)')

if [ -n "$SERVICE_URL" ]; then
    echo -e "\n[*] Updating service with CLOUD_RUN_URL: $SERVICE_URL"
    gcloud run services update smart-email-manager-agent --update-env-vars CLOUD_RUN_URL=$SERVICE_URL --region $REGION
fi

echo -e "\n✅ Smart Email Manager Stack Deployment Complete!"

echo -e "\n--- POST-DEPLOYMENT STEPS ---"
echo "[*] I am about to wire the Pub/Sub infrastructure and create the Gmail Watcher script."
read -p "Do you want to continue with automated infrastructure wiring? (y/n): " confirmation
if [[ "$confirmation" != "y" ]]; then
    echo "[!] Skipping infrastructure wiring. You will need to perform Step 4 manually."
    exit
fi

# 1. Get correct Service URL and Project Details
SERVICE_INFO=$(gcloud run services describe smart-email-manager-agent --platform managed --region $REGION)
SERVICE_URL=$(echo "$SERVICE_INFO" | grep -oP 'URL:\s+\K(https?://\S+)')

# Extract Project Number
PROJECT_NUMBER=$(echo "$SERVICE_INFO" | grep -oP 'projects/\K(\d+)(?=/locations)')
if [ -z "$PROJECT_NUMBER" ]; then
    PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
fi

echo -e "\nDetected GCP Project ID: $PROJECT_ID"
echo "Detected GCP Project Number: $PROJECT_NUMBER"

read -p "Use Project ID '$PROJECT_ID' and Number '$PROJECT_NUMBER' for Apps Script activation? (y/n): " confirm_project
if [[ "$confirm_project" != "y" ]]; then
    echo "[!] Skipping infrastructure wiring."
    exit
fi

# 2. Bridge A: Incoming Mail
echo -e "\n[*] Configuring Bridge A: Incoming Mail..."
gcloud pubsub topics create gmail-notifications 2>/dev/null
gcloud pubsub topics add-iam-policy-binding gmail-notifications \
    --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
    --role="roles/pubsub.publisher"

gcloud pubsub subscriptions create gmail-notifications-sub --topic=gmail-notifications \
    --push-endpoint="$SERVICE_URL/api/on-new-mail" 2>/dev/null || \
gcloud pubsub subscriptions update gmail-notifications-sub --push-endpoint="$SERVICE_URL/api/on-new-mail"

# 3. Bridge B: Auto-Reorganization
echo -e "\n[*] Configuring Bridge B: Auto-Reorganization..."
gcloud pubsub topics create reorganize-inbox 2>/dev/null
gcloud pubsub subscriptions create reorganize-inbox-sub --topic=reorganize-inbox \
    --push-endpoint="$SERVICE_URL/api/reorganize" 2>/dev/null || \
gcloud pubsub subscriptions update reorganize-inbox-sub --push-endpoint="$SERVICE_URL/api/reorganize"

# 4. Apps Script Automation
echo -e "\n[*] Creating Gmail Watcher Apps Script via clasp..."
mkdir -p "tmp-clasp-watch"
cd "tmp-clasp-watch"
clasp create --title "SEM-Gmail-Watcher" --type standalone 2>/dev/null
if [ $? -eq 0 ]; then
    cat <<EOF > Code.gs
function activateGmailWatch() { 
  GmailApp.getInboxUnreadCount(); 
  const projectId = '$PROJECT_ID'; 
  const options = { 
    method: 'post', 
    contentType: 'application/json', 
    payload: JSON.stringify({ 
      topicName: 'projects/' + projectId + '/topics/gmail-notifications', 
      labelIds: ['INBOX'] 
    }), 
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() }, 
    muteHttpExceptions: true 
  }; 
  const response = UrlFetchApp.fetch('https://gmail.googleapis.com/gmail/v1/users/me/watch', options); 
  Logger.log(response.getContentText()); 
}
EOF
    cat <<EOF > appsscript.json
{
  "timeZone": "America/New_York",
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "oauthScopes": [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/script.external_request"
  ]
}
EOF
    clasp push -f
fi
cd ..

echo -e "\n✅ INFRASTRUCTURE READY!"
echo "👉 FINAL STEPS TO ACTIVATE GMAIL WATCH:"
echo "1. Open your new script project: https://script.google.com/home/projects"
echo "2. Select 'SEM-Gmail-Watcher'."
echo "3. LINK TO GCP: Go to 'Settings' (gear icon) > 'Google Cloud Platform (GCP) Project' > 'Change Project'."
echo "4. PASTE THIS PROJECT NUMBER: $PROJECT_NUMBER"
echo "5. Click 'Set project'."
echo "6. RUN HANDSHAKE: Go back to 'Editor' (< > icon) > Select 'activateGmailWatch' > Click 'Run'."
echo "7. AUTHORIZE: Click 'Review Permissions' and follow the prompts."

echo -e "\n--- NEXT STEPS ---"
read -p "Would you like me to (1) Proceed with Step 5 (Permanent Autonomy) or (2) Start the SAM deployment? (1/2/q): " next_action
if [[ "$next_action" == "1" ]]; then
    echo "[*] Resolving Python dependencies..."
    pip install google-auth-oauthlib requests --quiet
    echo "[*] Launching Step 5: Permanent Autonomy Sync..."
    python3 "../permanent_auth.py"
elif [[ "$next_action" == "2" ]]; then
    echo "[*] Transitioning to SAM Deployment..."
fi
