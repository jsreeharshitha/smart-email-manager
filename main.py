from fastapi import FastAPI, Request, Body, HTTPException
from fastapi.responses import HTMLResponse
import os
import requests
from requests.auth import HTTPDigestAuth
import json
import uuid
import string
import random
from db.mongo_client import get_client
from config import settings
from toolbox import process_and_store_email
from tools.mongo_mcp import get_last_sync_timestamp, setup_database, find_unclassified_by_semantic_group, store_email_record
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id
from typing import List, Optional
from pydantic import BaseModel
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import service_usage_v1
from google.cloud import dialogflowcx_v3beta1 as dialogflow
from mcp.server.fastmcp import FastMCP

# --- 1. INITIALIZE FASTAPI & MCP ---
app = FastAPI(title="Smart Email Manager API")
mcp = FastMCP("SmartEmailManager")

# Register all tools with the MCP Server
# These are the "Superpowers" Gemini will use via Agent Builder
mcp.tool()(setup_database)
mcp.tool()(get_last_sync_timestamp)
mcp.tool()(find_unclassified_by_semantic_group)
mcp.tool()(store_email_record)
mcp.tool()(process_and_store_email)
mcp.tool()(create_label)
mcp.tool()(get_labels)
mcp.tool()(apply_label_to_email)
mcp.tool()(get_emails_by_id)

# --- 2. MCP OVER WEB (SSE ENDPOINT) ---
# This is how Vertex AI Agent Builder connects to your tools
@app.get("/mcp")
async def mcp_discovery():
    """Information about the MCP server."""
    return {
        "mcp_server": "SmartEmailManager",
        "status": "active",
        "transport": "SSE",
        "endpoint": "/mcp/sse"
    }

# Note: FastMCP usually handles its own routes. 
# We'll use the mcp.app (which is FastAPI) to serve everything.
app.mount("/mcp-server", mcp.app)

# --- 3. INFRASTRUCTURE PROVISIONING ---

class MongoSetupRequest(BaseModel):
    mongo_public_key: str
    mongo_private_key: str
    user_email: str

def generate_secure_password(length=16):
    characters = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(characters) for i in range(length))

def enable_gcp_api(project_id: str, service_name: str):
    """Programmatically enables a GCP service in the target project."""
    try:
        client = service_usage_v1.ServiceUsageClient()
        operation = client.enable_service(name=f"projects/{project_id}/services/{service_name}")
        print(f"Enabling API {service_name} for {project_id}...")
        operation.result()
    except Exception as e:
        print(f"API Enablement Warning: {str(e)}")

def setup_agent_playbook(project_id: str, agent_id: str, location: str = "global"):
    """
    Provisions a Playbook for the Generative AI Agent.
    Defines the goals, instructions, and reasoning logic.
    """
    client = dialogflow.PlaybooksClient(
        client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"}
    )
    
    parent = f"projects/{project_id}/locations/{location}/agents/{agent_id}"
    
    playbook = dialogflow.Playbook(
        display_name="Smart Email Management Playbook",
        goal="Automatically organize, label, and summarize the user's Gmail inbox using semantic reasoning.",
        instruction=dialogflow.Playbook.Instruction(
            steps=[
                dialogflow.Playbook.Step(text="Greet the user and explain your purpose as an AI Email Manager."),
                dialogflow.Playbook.Step(text="Use the 'get_last_sync_timestamp' tool to check the status of the mailbox."),
                dialogflow.Playbook.Step(text="If new emails are detected, use 'process_and_store_email' to index them into MongoDB."),
                dialogflow.Playbook.Step(text="Analyze unclassified emails using 'find_unclassified_by_semantic_group' to identify patterns."),
                dialogflow.Playbook.Step(text="If a strong semantic group is found (e.g., 'Travel', 'Work'), use 'create_label' to organize the inbox."),
                dialogflow.Playbook.Step(text="Apply newly created labels to matching messages using 'apply_label_to_email'."),
                dialogflow.Playbook.Step(text="Provide a summary of the organizational changes to the user.")
            ]
        )
    )

    request = dialogflow.CreatePlaybookRequest(
        parent=parent,
        playbook=playbook
    )

    response = client.create_playbook(request=request)
    print(f"Created Playbook: {response.name}")
    return response.name

def setup_agent_builder(project_id: str, location: str = "global"):
    """Provisions Vertex AI Agent Builder Data Store and Engine."""
    # 1. Enable APIs
    enable_gcp_api(project_id, "discoveryengine.googleapis.com")
    enable_gcp_api(project_id, "dialogflow.googleapis.com")

    # 2. Create Data Store
    ds_client = discoveryengine.DataStoreServiceClient()
    ds_id = f"email-ds-{uuid.uuid4().hex[:6]}"
    
    data_store = discoveryengine.DataStore(
        display_name="Email Knowledge Base",
        industry_vertical=discoveryengine.DataStore.IndustryVertical.GENERIC,
        content_config=discoveryengine.DataStore.ContentConfig.CONTENT_REQUIRED,
    )

    parent = f"projects/{project_id}/locations/{location}/collections/default_collection"
    ds_operation = ds_client.create_data_store(parent=parent, data_store=data_store, data_store_id=ds_id)
    ds_operation.result()

    # 3. Create Engine (The Agent App)
    engine_client = discoveryengine.EngineServiceClient()
    engine_id = f"email-agent-{uuid.uuid4().hex[:6]}"
    
    engine = discoveryengine.Engine(
        display_name="Smart Email Manager",
        solution_type=discoveryengine.Engine.SolutionType.CHAT,
        data_store_ids=[ds_id],
        chat_engine_config=discoveryengine.Engine.ChatEngineConfig(
            agent_config=discoveryengine.Engine.ChatEngineConfig.AgentConfig(
                language_code="en",
                time_zone="UTC"
            )
        )
    )

    engine_operation = engine_client.create_engine(parent=parent, engine=engine, engine_id=engine_id)
    engine_operation.result()

    # 4. Create the Playbook (The Instructions)
    # Note: In Agent Builder, the engine_id acts as the agent_id
    try:
        playbook_name = setup_agent_playbook(project_id, engine_id, location)
    except Exception as e:
        print(f"Playbook Creation Error: {str(e)}")
        playbook_name = "manual-setup-required"

    return {"data_store_id": ds_id, "engine_id": engine_id, "playbook_name": playbook_name}

@app.post("/api/setup-db")
async def setup_infrastructure(setup_request: MongoSetupRequest):
    """Orchestrates MongoDB and Agent Builder setup."""
    public_key = setup_request.mongo_public_key
    private_key = setup_request.mongo_private_key
    user_email = setup_request.user_email
    project_id = os.environ.get("PROJECT_ID", "grah-2026") 
    
    auth = HTTPDigestAuth(public_key, private_key)
    headers = {"Accept": "application/vnd.atlas.2023-01-01+json", "Content-Type": "application/json"}

    try:
        # 1. MongoDB Setup
        orgs_res = requests.get("https://cloud.mongodb.com/api/atlas/v2/orgs", auth=auth, headers=headers)
        org_id = orgs_res.json()["results"][0]["id"]
        
        project_name = f"Rapid-Agent-{uuid.uuid4().hex[:6]}"
        project_res = requests.post("https://cloud.mongodb.com/api/atlas/v2/groups", 
                                    auth=auth, headers=headers, json={"name": project_name, "orgId": org_id})
        mongo_project_id = project_res.json()["id"]

        # IP Whitelist & DB User
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/accessList",
                      auth=auth, headers=headers, json=[{"ipAddress": "0.0.0.0/0"}])

        db_pass = generate_secure_password()
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/databaseUsers",
                      auth=auth, headers=headers, json={
                          "databaseName": "admin", "password": db_pass, "username": "agent_user",
                          "roles": [{"databaseName": "smart_email_manager", "roleName": "readWrite"}]
                      })

        # M0 Cluster
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/clusters",
                      auth=auth, headers=headers, json={
                          "name": "email-cluster", "clusterType": "REPLICASET",
                          "providerSettings": {"providerName": "TENANT", "backingProviderName": "GCP", 
                                              "instanceSizeName": "M0", "regionName": "CENTRAL_US"}
                      })

        # 2. Agent Builder Setup
        agent_builder_info = setup_agent_builder(project_id)

        return {
            "status": "In-Progress", 
            "message": f"Provisioning {project_name} and Vertex AI Agent...",
            "mongo_project_id": mongo_project_id,
            "agent_builder_app": agent_builder_info["engine_id"],
            "mcp_url": f"{os.environ.get('CLOUD_RUN_URL', 'https://agent.run.app')}/mcp-server/sse"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Setup failed: {str(e)}")

# --- 4. LEGACY ENDPOINTS (For Add-on Compatibility) ---

@app.get("/")
async def root():
    return {
        "message": "Smart Email Manager Agent is running.",
        "mcp_status": "active",
        "mcp_endpoint": "/mcp-server/sse"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
