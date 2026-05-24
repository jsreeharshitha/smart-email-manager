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

## Quickstart

### Prerequisites

- Python 3.11+
- A Google Cloud Project with Gmail API enabled.
- A MongoDB Atlas Cluster (Free tier works great).
- Google Cloud SDK (`gcloud`) installed and configured.

### Local Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/jsreeharshitha/smart-email-manager.git
    cd smart-email-manager
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure Environment Variables:**
    Create a `.env` file (or set them in your environment):
    ```env
    MONGO_URI=your_mongodb_atlas_connection_string
    VOYAGE_API_KEY=your_voyage_ai_api_key
    ```

4.  **Google API Setup:**
    - Place your `credentials.json` (OAuth 2.0 Client ID) from the Google Cloud Console in the root directory.
    - Run the agent for the first time to complete the OAuth flow:
      ```bash
      python tools/gmail_mcp.py
      ```
    - This will generate a `token.json` file for subsequent runs.

5.  **Initialize Vector Index:**
    ```bash
    python tools/setup_vector_index.py
    ```

6.  **Run the Agent:**
    ```bash
    python main.py
    ```

### Deployment (Cloud Run)

Use the provided scripts to deploy to Google Cloud Run:

**Linux/macOS:**
```bash
chmod +x deploy/deploy.sh
./deploy/deploy.sh
```

**Windows (PowerShell):**
```powershell
.\deploy\deploy.ps1
```

## Architecture

![Architecture](smart-email-manager-agent-architecture.png)

The agent uses:
- **FastAPI:** To provide a web interface and OAuth callback endpoints.
- **FastMCP:** To bridge the AI agent with Gmail tools.
- **MongoDB Atlas:** For data persistence and vector search.
- **Voyage AI:** For high-quality text embeddings.
