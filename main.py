import os
import requests
from requests.auth import HTTPDigestAuth
import json
import uuid
import string
import random
from db.mongo_client import get_client
from config import settings
from toolbox import process_and_store_email, cluster_unclassified_emails
from tools.mongo_mcp import get_last_sync_timestamp, setup_database, find_unclassified_by_semantic_group, store_email_record
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id
from typing import List, Optional
from pydantic import BaseModel
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import service_usage_v1
from google.cloud import dialogflowcx_v3beta1 as dialogflow
from google.cloud import pubsub_v1
from mcp.server.fastmcp import FastMCP
from datetime import datetime, UTC
from fastapi import Request, HTTPException

# --- 1. INITIALIZE MCP SERVER ---
# FastMCP acts as our primary ASGI application
app = FastMCP("SmartEmailManager")

# Register all tools with the MCP Server
# These are the "Superpowers" Gemini will use via Agent Builder
app.tool()(setup_database)
app.tool()(get_last_sync_timestamp)
app.tool()(find_unclassified_by_semantic_group)
app.tool()(store_email_record)
app.tool()(process_and_store_email)
app.tool()(cluster_unclassified_emails)
app.tool()(create_label)
app.tool()(get_labels)
app.tool()(apply_label_to_email)
app.tool()(get_emails_by_id)

# --- 2. INFRASTRUCTURE PROVISIONING ---

class MongoSetupRequest(BaseModel):
    mongo_public_key: str
    mongo_private_key: str
    user_email: str
    gmail_token: str

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
    """Provisions a Playbook for the Generative AI Agent."""
    client = dialogflow.PlaybooksClient(
        client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"}
    )
    parent = f"projects/{project_id}/locations/{location}/agents/{agent_id}"
    
    playbook = dialogflow.Playbook(
        display_name="Smart Email Management Playbook",
        goal="Automatically organize, label, and summarize the user's Gmail inbox using semantic reasoning and autonomous category discovery.",
        instruction=dialogflow.Playbook.Instruction(
            steps=[
                dialogflow.Playbook.Step(text="Greet the user and explain your purpose as an AI Email Manager."),
                dialogflow.Playbook.Step(text="Use the 'get_last_sync_timestamp' tool to check the status of the mailbox."),
                dialogflow.Playbook.Step(text="If new emails are detected, use 'process_and_store_email' to index them into MongoDB."),
                dialogflow.Playbook.Step(text="Proactively discover new categories by using the 'cluster_unclassified_emails' tool."),
                dialogflow.Playbook.Step(text="For each identified cluster, analyze the representative email snippets to propose a 1-3 word Gmail label."),
                dialogflow.Playbook.Step(text="Ask the user for permission before creating and applying new labels."),
                dialogflow.Playbook.Step(text="If authorized, use 'create_label' and 'apply_label_to_email' to organize the inbox."),
                dialogflow.Playbook.Step(text="Provide a summary of the organizational changes to the user.")
            ]
        )
    )

    request = dialogflow.CreatePlaybookRequest(parent=parent, playbook=playbook)
    response = client.create_playbook(request=request)
    return response.name

def setup_agent_builder(project_id: str, location: str = "global"):
    """Provisions Vertex AI Agent Builder Data Store and Engine."""
    enable_gcp_api(project_id, "discoveryengine.googleapis.com")
    enable_gcp_api(project_id, "dialogflow.googleapis.com")

    # Data Store
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

    # Engine
    engine_client = discoveryengine.EngineServiceClient()
    engine_id = f"email-agent-{uuid.uuid4().hex[:6]}"
    engine = discoveryengine.Engine(
        display_name="Smart Email Manager",
        solution_type=discoveryengine.Engine.SolutionType.CHAT,
        data_store_ids=[ds_id],
        chat_engine_config=discoveryengine.Engine.ChatEngineConfig(
            agent_config=discoveryengine.Engine.ChatEngineConfig.AgentConfig(
                language_code="en", time_zone="UTC"
            )
        )
    )
    engine_operation = engine_client.create_engine(parent=parent, engine=engine, engine_id=engine_id)
    engine_operation.result()

    try:
        playbook_name = setup_agent_playbook(project_id, engine_id, location)
    except Exception as e:
        print(f"Playbook Creation Error: {str(e)}")
        playbook_name = "manual-setup-required"

    return {"data_store_id": ds_id, "engine_id": engine_id, "playbook_name": playbook_name}

def setup_pubsub(project_id: str):
    """Sets up Pub/Sub for Gmail Push Notifications."""
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    
    topic_id = "gmail-notifications"
    topic_path = publisher.topic_path(project_id, topic_id)
    
    try:
        publisher.create_topic(name=topic_path)
    except Exception:
        pass # Already exists

    # Grant Gmail Permission
    policy = publisher.get_iam_policy(request={"resource": topic_path})
    gmail_sa = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
    if not any(gmail_sa in b.members for b in policy.bindings if b.role == "roles/pubsub.publisher"):
        publisher.set_iam_policy(request={
            "resource": topic_path,
            "policy": {"bindings": [{"role": "roles/pubsub.publisher", "members": [gmail_sa]}]}
        })

    # Push Subscription
    sub_id = "gmail-notifications-sub"
    sub_path = subscriber.subscription_path(project_id, sub_id)
    push_url = f"{os.environ.get('CLOUD_RUN_URL', 'https://agent.run.app')}/api/on-new-mail"
    
    try:
        subscriber.create_subscription(request={
            "name": sub_path, "topic": topic_path,
            "push_config": {"push_endpoint": push_url},
            "ack_deadline_seconds": 60
        })
    except Exception:
        subscriber.update_subscription(request={
            "subscription": {"name": sub_path, "push_config": {"push_endpoint": push_url}},
            "update_mask": {"paths": ["push_config"]}
        })

    return topic_path

# Standard web routes added directly to the FastMCP app
@app.post("/api/setup-db")
async def setup_infrastructure(setup_request: MongoSetupRequest):
    """Orchestrates full Enterprise AI stack setup."""
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

        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/accessList",
                      auth=auth, headers=headers, json=[{"ipAddress": "0.0.0.0/0"}])

        db_pass = generate_secure_password()
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/databaseUsers",
                      auth=auth, headers=headers, json={
                          "databaseName": "admin", "password": db_pass, "username": "agent_user",
                          "roles": [{"databaseName": "smart_email_manager", "roleName": "readWrite"}]
                      })

        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/clusters",
                      auth=auth, headers=headers, json={
                          "name": "email-cluster", "clusterType": "REPLICASET",
                          "providerSettings": {"providerName": "TENANT", "backingProviderName": "GCP", 
                                              "instanceSizeName": "M0", "regionName": "CENTRAL_US"}
                      })

        # 2. Agent Builder Setup
        agent_builder_info = setup_agent_builder(project_id)

        # 3. Pub/Sub Setup
        pubsub_topic = setup_pubsub(project_id)

        # 4. Persist Credentials to MongoDB
        client = get_client()
        db = client["smart_email_manager"]
        db["UserSessions"].update_one(
            {"user_email": user_email},
            {
                "$set": {
                    "status": "ready",
                    "mongo_project_id": mongo_project_id,
                    "agent_builder": agent_builder_info,
                    "credentials": {
                        "access_token": setup_request.gmail_token,
                    },
                    "updated_at": datetime.now(UTC).isoformat()
                }
            },
            upsert=True
        )

        return {
            "status": "In-Progress", 
            "message": f"Successfully initiated Enterprise AI Stack setup.",
            "mongo_project_id": mongo_project_id,
            "agent_builder_app": agent_builder_info["engine_id"],
            "pubsub_topic": pubsub_topic,
            "mcp_url": f"{os.environ.get('CLOUD_RUN_URL', 'https://agent.run.app')}/mcp/sse"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Setup failed: {str(e)}")

@app.post("/api/on-new-mail")
async def handle_new_mail(request: Request):
    """Receives push notifications from Gmail via Pub/Sub."""
    envelope = await request.json()
    if not envelope:
        return {"status": "error", "message": "Missing envelope"}
    
    import base64
    pubsub_message = envelope.get("message", {})
    data_str = base64.b64decode(pubsub_message.get("data", "")).decode("utf-8")
    notification = json.loads(data_str)
    
    user_email = notification.get("emailAddress")
    new_history_id = notification.get("historyId")
    
    print(f"Notification received for {user_email}. New History ID: {new_history_id}")

    client = get_client()
    db = client["smart_email_manager"]
    user_session = db["UserSessions"].find_one({"user_email": user_email})

    if not user_session:
        return {"status": "ignored", "message": "User session not found"}

    last_history_id = user_session.get("last_history_id")
    
    from tools.gmail_mcp import get_gmail_service
    gmail = get_gmail_service(user_email)

    if not last_history_id:
        db["UserSessions"].update_one(
            {"user_email": user_email},
            {"$set": {"last_history_id": new_history_id}}
        )
        return {"status": "initialized", "history_id": new_history_id}

    history_res = gmail.users().history().list(
        userId="me",
        startHistoryId=last_history_id,
        historyTypes=["messageAdded"]
    ).execute()

    changes = history_res.get("history", [])
    new_messages_count = 0

    for change in changes:
        messages_added = change.get("messagesAdded", [])
        for item in messages_added:
            msg_id = item.get("message", {}).get("id")
            if not msg_id:
                continue

            msg_detail = gmail.users().messages().get(userId="me", id=msg_id).execute()
            headers = msg_detail.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
            sender = next((h["value"] for h in headers if h["name"] == "From"), "Unknown")
            date = next((h["value"] for h in headers if h["name"] == "Date"), "")
            
            metadata = {
                "subject": subject,
                "sender": sender,
                "date": date,
                "message_id": msg_id,
                "user_email": user_email
            }
            body = msg_detail.get("snippet", "")
            
            process_and_store_email(metadata, body)
            new_messages_count += 1
            print(f"Indexed new email: {subject} (ID: {msg_id})")

    db["UserSessions"].update_one(
        {"user_email": user_email},
        {"$set": {"last_history_id": new_history_id}}
    )

    return {
        "status": "success",
        "processed": new_messages_count,
        "history_id": new_history_id
    }

@app.get("/")
async def root():
    return {
        "message": "Smart Email Manager Agent is running.",
        "mcp_status": "active",
        "mcp_endpoint": "/mcp/sse"
    }

if __name__ == "__main__":
    # Use the FastMCP run method which handles the server
    # Or use uvicorn on the app object directly
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    # Note: FastMCP is an ASGI app, so we run it directly
    uvicorn.run(app, host="0.0.0.0", port=port)
