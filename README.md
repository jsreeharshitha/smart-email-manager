# Smart Email Manager

Automate your inbox using the power of MongoDB Atlas, Vector Search, and Agentic AI. This project provides a "one-click" deployment for a smart email organizer that semantically classifies and groups your emails.

## Description

Smart Email Manager is an AI-powered agent designed to help you regain control over your Gmail inbox. It uses MongoDB Atlas Vector Search to understand the semantic meaning of your emails and automatically applies labels, groups related messages, and helps you prioritize your communication.

Built for the **Google Rapid Agent Hackathon**, this agent leverages the Model Context Protocol (MCP) to interact directly with Gmail, while using MongoDB as a long-term semantic memory and storage engine.

## Key Features

- **Semantic Classification:** Automatically categorize emails based on their content, not just keywords.
- **Vector Search:** Find related emails using semantic similarity with MongoDB Atlas Vector Search.
- **Gmail Integration:** Seamlessly interacts with Gmail to create labels, apply them to messages, and fetch email data.
- **Cloud Run Ready:** Containerized and ready for deployment on Google Cloud Run with automated scripts.
- **MongoDB Atlas Integration:** Uses MongoDB for storing email metadata and vector embeddings for fast, scalable semantic lookups.

## Use Cases

1.  **Automated Inbox Organization:** Automatically label incoming invoices, project updates, or travel confirmations.
2.  **Semantic Email Search:** Search for "financial documents" and find invoices, receipts, and bank statements even if they don't contain the word "financial".
3.  **Intelligent Email Grouping:** Group unclassified emails that share similar semantic themes for batch processing.
4.  **Customer Support Triage:** Automatically tag support emails by topic (e.g., "billing", "technical-issue", "feature-request").

## Screenshots

| Dashboard | Components |
| :---: | :---: |
| ![Smart Email Manager](screenshot/smart-email-manager.png) | ![Components](screenshot/smart-email-manager-components.png) |

## Setup & Deployment Guide (Local & Cloud)

This guide provides the comprehensive path to deploy the Smart Email Manager stack.

### Step 0: Prerequisites

Before you begin, ensure you have the following accounts and keys:

1.  **MongoDB Atlas**:
    - Create a free account at [mongodb.com/atlas](https://www.mongodb.com/cloud/atlas/register).
    - **Create a Project** and a **Cluster** (Shared/Free Tier).
    - **Database Access**: Create a user with **"Read and write to any database"** permissions.
    - **Network Access**: Add **`0.0.0.0/0`** (Allow Access from Anywhere) for development.
    - **Connect**: Copy your **Connection String (MONGO_URI)**.

2.  **Voyage AI**:
    - Sign up at [voyageai.com](https://www.voyageai.com/).
    - Go to **API Keys** and copy your **API Key**.

### Step 1: Initialize Environment

1.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Environment Variables:**
    Create a `.env` file or set them in your environment:
    ```env
    MONGO_URI=your_mongodb_atlas_connection_string
    VOYAGE_API_KEY=your_voyage_ai_api_key
    ```

3.  **Google API Setup:**
    - Place your `credentials.json` (OAuth 2.0 Client ID) in the root directory.
    - Run the agent to complete the OAuth flow:
      ```bash
      python tools/gmail_mcp.py
      ```

### Step 2: Provision Identity & Permissions (GCP)

If deploying to Google Cloud, follow these steps:

1.  **Grant Deployment Roles**:
    ```bash
    PROJECT_ID=$(gcloud config get-value project)
    USER_EMAIL=$(gcloud config get-value account)

    gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$USER_EMAIL" --role="roles/run.admin"
    gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$USER_EMAIL" --role="roles/run.viewer"
    gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$USER_EMAIL" --role="roles/discoveryengine.admin"
    gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$USER_EMAIL" --role="roles/pubsub.admin"
    gcloud projects add-iam-policy-binding $PROJECT_ID --member="user:$USER_EMAIL" --role="roles/aiplatform.user"
    ```

### Step 3: Deploy Backend (Cloud Run)

1.  **Build Container**:
    Initialize variables and create the repository:
    ```bash
    PROJECT_ID=$(gcloud config get-value project)
    CHOSEN_REGION=${CHOSEN_REGION:-us-central1}

    gcloud artifacts repositories create agent-repo \
        --repository-format=docker \
        --location=$CHOSEN_REGION || true
    ```

    **Build and Tag:**
    ```bash
    gcloud builds submit --tag $CHOSEN_REGION-docker.pkg.dev/$PROJECT_ID/agent-repo/smart-email-manager-agent .
    ```

2.  **Launch Service**:
    **Deploy the agent:**
    ```bash
    gcloud run deploy smart-email-manager-agent \
      --image $CHOSEN_REGION-docker.pkg.dev/$PROJECT_ID/agent-repo/smart-email-manager-agent \
      --platform managed --region $CHOSEN_REGION --allow-unauthenticated --port 8080
    ```

    **Update service with its own URL:**
    ```bash
    SERVICE_URL=$(gcloud run services describe smart-email-manager-agent --platform managed --region $CHOSEN_REGION --format='value(status.url)')
    gcloud run services update smart-email-manager-agent --set-env-vars CLOUD_RUN_URL=$SERVICE_URL --region $CHOSEN_REGION
    ```

3.  **Configure Secrets**:
    ```bash
    export MONGO_URI="your_mongodb_atlas_uri"
    export VOYAGE_API_KEY="your_voyage_ai_api_key"

    gcloud run services update smart-email-manager-agent \
      --set-env-vars="MONGO_URI=$MONGO_URI" \
      --set-env-vars="VOYAGE_API_KEY=$VOYAGE_API_KEY" \
      --region $CHOSEN_REGION
    ```

### Step 4: Provision Infra (Pub/Sub & Vertex AI)

1.  **Provision Vertex AI Search**:
    1. **Enable API:**
    ```bash
    gcloud services enable discoveryengine.googleapis.com
    ```
    2. **Create Placeholder Data Store:**
    ```bash
    curl -X POST \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      -H "X-Goog-User-Project: $(gcloud config get-value project)" \
      -H "Content-Type: application/json" \
      -d '{"displayName": "Smart Email Manager Data Store", "industryVertical": "GENERIC", "contentConfig": "NO_CONTENT"}' \
      "https://discoveryengine.googleapis.com/v1beta/projects/$(gcloud config get-value project)/locations/global/collections/default_collection/dataStores?dataStoreId=smart-email-manager-ds"
    ```
    3. **Create Search Engine:**
    ```bash
    curl -X POST \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      -H "X-Goog-User-Project: $(gcloud config get-value project)" \
      -H "Content-Type: application/json" \
      -d '{"displayName": "Smart Email Manager", "solutionType": "SOLUTION_TYPE_SEARCH", "industryVertical": "GENERIC", "dataStoreIds": ["smart-email-manager-ds"]}' \
      "https://discoveryengine.googleapis.com/v1beta/projects/$(gcloud config get-value project)/locations/global/collections/default_collection/engines?engineId=smart-email-manager"
    ```

2.  **Create Pub/Sub Bridges**:
    **Bridge A:** Incoming Mail
    ```bash
    gcloud pubsub topics create gmail-notifications || true
    gcloud pubsub topics add-iam-policy-binding gmail-notifications \
        --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
        --role="roles/pubsub.publisher"
    gcloud pubsub subscriptions create gmail-notifications-sub --topic=gmail-notifications \
        --push-endpoint="$SERVICE_URL/api/on-new-mail" || \
    gcloud pubsub subscriptions update gmail-notifications-sub --push-endpoint="$SERVICE_URL/api/on-new-mail"
    ```

    **Bridge B:** Auto-Reorganization
    ```bash
    gcloud pubsub topics create reorganize-inbox || true
    gcloud pubsub subscriptions create reorganize-inbox-sub --topic=reorganize-inbox \
        --push-endpoint="$SERVICE_URL/api/reorganize" || \
    gcloud pubsub subscriptions update reorganize-inbox-sub --push-endpoint="$SERVICE_URL/api/reorganize"
    ```

3.  **Activate Gmail Watch (The Handshake)**:
    1. **Pre-flight**: Go to [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent). Set **User Type: External**. Under **Test users**, add your email and click **SAVE**.
    2. Open your [**Apps Script Editor**](https://script.google.com/). Paste and **Run** the `activateGmailWatch` function:
    ```js
    function activateGmailWatch() {
      GmailApp.getInboxUnreadCount(); // Triggers permission prompt
      const projectId = "YOUR_PROJECT_ID";
      const options = {
        method: "post",
        contentType: "application/json",
        payload: JSON.stringify({
          topicName: `projects/${projectId}/topics/gmail-notifications`,
          labelIds: ["INBOX"],
        }),
        headers: { Authorization: "Bearer " + ScriptApp.getOAuthToken() },
        muteHttpExceptions: true,
      };
      const response = UrlFetchApp.fetch(
        "https://gmail.googleapis.com/gmail/v1/users/me/watch",
        options,
      );
      Logger.log(response.getContentText());
    }
    ```

### Step 5: Final Polish (Permanent Autonomy)

This final step grants your agent a persistent **Refresh Token** so it never expires.

1.  **Create Desktop Client**:
    - Go to [APIs & Services > Credentials](https://console.cloud.google.com/apis/credentials).
    - Click **CREATE CREDENTIALS** > **OAuth client ID** (Application type: Desktop app).
    - Download the JSON for the new client.
2.  **Sync Credentials**:
    - Upload the `.json` file to the `agents/smart-email-manager/` folder (rename to `client_secret.json`).
    - Run the sync automation:
    ```bash
    pip install google-auth-oauthlib requests --quiet
    python3 permanent_auth.py
    ```
3.  **Follow Prompts**: Visit the URL, authorize, and paste the broken localhost URL back into the terminal.

**DONE!** 🟢 Your agent is now immortal and watching your inbox in the Gmail Sidebar.

## Architecture

![Architecture](smart-email-manager-agent-architecture.png)

The agent uses:
- **FastAPI:** To provide a web interface and OAuth callback endpoints.
- **FastMCP:** To bridge the AI agent with Gmail tools.
- **MongoDB Atlas:** For data persistence and vector search.
- **Voyage AI:** For high-quality text embeddings.
