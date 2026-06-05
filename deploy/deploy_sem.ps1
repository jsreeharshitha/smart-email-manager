# Master Deployment Script: Smart Email Manager Stack
# --------------------------------------------------
$PROJECT_ID = gcloud config get-value project
$REGION = if ($env:CHOSEN_REGION) { $env:CHOSEN_REGION } else { "us-central1" }
$IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/agent-repo/smart-email-manager-agent:latest"

# 0. Load Secrets from .env (CASCADING: submodule -> super-repo root)
$envPaths = @("../.env", "../../../.env")
foreach ($path in $envPaths) {
    if (Test-Path $path) {
        Write-Host "[*] Loading environment variables from $path..."
        Get-Content $path | ForEach-Object {
            if ($_ -match "^(?<name>[^=]+)=(?<value>['""]?)(?<val>.*)\k<value>$") {
                $varName = $Matches['name']
                $varVal = $Matches['val']
                # Only set if not already set by a more local .env
                if (-not $env:$varName) {
                    Set-Item -Path "env:$varName" -Value $varVal
                }
            }
        }
    }
}

# 1. Provision Vertex AI Infrastructure
$TOKEN = gcloud auth print-access-token

Write-Host "`n[1/3] Provisioning Vertex AI Data Store..."
curl.exe -X POST `
      -H "Authorization: Bearer $TOKEN" `
      -H "X-Goog-User-Project: $PROJECT_ID" `
      -H "Content-Type: application/json" `
      -d "@datastore_payload.json" `
      "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/dataStores?dataStoreId=smart-email-manager-ds"

Write-Host "`n[2/3] Provisioning Vertex AI Search Engine..."
curl.exe -X POST `
      -H "Authorization: Bearer $TOKEN" `
      -H "X-Goog-User-Project: $PROJECT_ID" `
      -H "Content-Type: application/json" `
      -d "@engine_payload.json" `
      "https://discoveryengine.googleapis.com/v1beta/projects/$PROJECT_ID/locations/global/collections/default_collection/engines?engineId=smart-email-manager"

# 2. Build and Deploy SEM Agent
Write-Host "`n[3/4] Building and Pushing SEM Agent Image..."
gcloud builds submit --tag $IMAGE ..

Write-Host "`n[4/4] Deploying SEM Agent to Cloud Run..."
gcloud run deploy smart-email-manager-agent `
    --image $IMAGE `
    --platform managed --region $REGION --allow-unauthenticated `
    --memory 4Gi --cpu 1 --concurrency 10 `
    --set-env-vars="MONGO_URI=$($env:MONGO_URI),VOYAGE_API_KEY=$($env:VOYAGE_API_KEY),PROJECT_ID=$PROJECT_ID"

# 3. Post-Deployment: Set CLOUD_RUN_URL
$service_info = gcloud run services describe smart-email-manager-agent --platform managed --region $REGION
$service_info_str = $service_info -join "`n"
if ($service_info_str -match "URL:\s+(https?://\S+)") {
    $SERVICE_URL = $Matches[1]
    Write-Host "`n[*] Updating service with CLOUD_RUN_URL: $SERVICE_URL"
    gcloud run services update smart-email-manager-agent --update-env-vars "CLOUD_RUN_URL=$SERVICE_URL" --region $REGION
}

Write-Host "`n✅ Smart Email Manager Stack Deployment Complete!"

Write-Host "`n--- POST-DEPLOYMENT STEPS ---"
Write-Host "[*] I am about to wire the Pub/Sub infrastructure and create the Gmail Watcher script."
$confirmation = Read-Host "Do you want to continue with automated infrastructure wiring? (y/n)"
if ($confirmation -ne 'y') {
    Write-Host "[!] Skipping infrastructure wiring. You will need to perform Step 4 manually."
    exit
}

# 1. Get correct Service URL and Project Details using robust parsing
$service_info = gcloud run services describe smart-email-manager-agent --platform managed --region $REGION
$service_info_str = $service_info -join "`n"
if ($service_info_str -match "URL:\s+(https?://\S+)") {
    $SERVICE_URL = $Matches[1]
}

# Extract Project Number from the Service URL or Metadata
# Usually URLs look like: https://service-name-<PROJECT_NUMBER>.<REGION>.run.app
if ($service_info_str -match "projects/(\d+)/locations") {
    $PROJECT_NUMBER = $Matches[1]
} else {
    $PROJECT_NUMBER = gcloud projects describe $PROJECT_ID --format='value(projectNumber)'
}

Write-Host "`nDetected GCP Project ID: $PROJECT_ID"
Write-Host "Detected GCP Project Number: $PROJECT_NUMBER"

$confirm_project = Read-Host "Use Project ID '$PROJECT_ID' and Number '$PROJECT_NUMBER' for Apps Script activation? (y/n)"
if ($confirm_project -ne 'y') {
    Write-Host "[!] Skipping infrastructure wiring."
    exit
}

# 2. Bridge A: Incoming Mail
Write-Host "`n[*] Configuring Bridge A: Incoming Mail..."
gcloud pubsub topics create gmail-notifications 2>$null
gcloud pubsub topics add-iam-policy-binding gmail-notifications `
    --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" `
    --role="roles/pubsub.publisher"

gcloud pubsub subscriptions create gmail-notifications-sub --topic=gmail-notifications `
    --push-endpoint="$SERVICE_URL/api/on-new-mail" 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud pubsub subscriptions update gmail-notifications-sub --push-endpoint="$SERVICE_URL/api/on-new-mail"
}

# 3. Bridge B: Auto-Reorganization
Write-Host "`n[*] Configuring Bridge B: Auto-Reorganization..."
gcloud pubsub topics create reorganize-inbox 2>$null
gcloud pubsub subscriptions create reorganize-inbox-sub --topic=reorganize-inbox `
    --push-endpoint="$SERVICE_URL/api/reorganize" 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud pubsub subscriptions update reorganize-inbox-sub --push-endpoint="$SERVICE_URL/api/reorganize"
}

# 4. Apps Script Automation
Write-Host "`n[*] Creating Gmail Watcher Apps Script via clasp..."
# Note: Assumes clasp is installed and user is logged in
if (-not (Test-Path "tmp-clasp-watch")) {
    New-Item -ItemType Directory -Force -Path "tmp-clasp-watch" | Out-Null
}
Push-Location "tmp-clasp-watch"
clasp create --title "SEM-Gmail-Watcher" --type standalone 2>$null
if ($LASTEXITCODE -eq 0) {
    # Script created successfully, now push code
    Set-Content -Path "Code.gs" -Value "function activateGmailWatch() { GmailApp.getInboxUnreadCount(); const projectId = '$PROJECT_ID'; const options = { method: 'post', contentType: 'application/json', payload: JSON.stringify({ topicName: 'projects/' + projectId + '/topics/gmail-notifications', labelIds: ['INBOX'] }), headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() }, muteHttpExceptions: true }; const response = UrlFetchApp.fetch('https://gmail.googleapis.com/gmail/v1/users/me/watch', options); Logger.log(response.getContentText()); }"
    Set-Content -Path "appsscript.json" -Value '{ "timeZone": "America/New_York", "exceptionLogging": "STACKDRIVER", "runtimeVersion": "V8", "oauthScopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/script.external_request"] }'
    clasp push -f
}
Pop-Location

Write-Host "`n✅ INFRASTRUCTURE READY!"
Write-Host "👉 FINAL STEPS TO ACTIVATE GMAIL WATCH:"
Write-Host "1. Open your new script project: https://script.google.com/home/projects"
Write-Host "2. Select 'SEM-Gmail-Watcher'."
Write-Host "3. LINK TO GCP: Go to 'Settings' (gear icon) > 'Google Cloud Platform (GCP) Project' > 'Change Project'."
Write-Host "4. PASTE THIS PROJECT NUMBER: $PROJECT_NUMBER"
Write-Host "5. Click 'Set project'."
Write-Host "6. RUN HANDSHAKE: Go back to 'Editor' (< > icon) > Select 'activateGmailWatch' > Click 'Run'."
Write-Host "7. AUTHORIZE: Click 'Review Permissions' and follow the prompts."

Write-Host "`n--- NEXT STEPS ---"
$next_action = Read-Host "Would you like me to (1) Proceed with Step 5 (Permanent Autonomy) or (2) Start the SAM deployment? (1/2/q)"
if ($next_action -eq "1") {
    Write-Host "[*] Resolving Python dependencies..."
    pip install google-auth-oauthlib requests --quiet
    Write-Host "[*] Launching Step 5: Permanent Autonomy Sync..."
    python "./permanent_auth.py"
} elseif ($next_action -eq "2") {
    Write-Host "[*] Transitioning to SAM Deployment..."
}
