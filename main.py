import os
import requests
from requests.auth import HTTPDigestAuth
import json
import uuid
import string
import random
import traceback
from db.mongo_client import get_client, get_collection
from config import settings
from toolbox import process_and_store_email, cluster_unclassified_emails
from tools.mongo_mcp import get_last_sync_timestamp, setup_database, find_unclassified_by_semantic_group, store_email_record
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id
from typing import List, Optional
from pydantic import BaseModel
from google.cloud import discoveryengine_v1beta as discoveryengine
from google.cloud import service_usage_v1
from google.cloud import dialogflowcx_v3beta1 as dialogflow
from google.cloud import pubsub_v1
from datetime import datetime, UTC
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import Response

# --- 1. INITIALIZE FASTAPI ---
app = FastAPI(title="Smart Email Manager API")

TOOLS = {
    "setup_database": setup_database,
    "get_last_sync_timestamp": get_last_sync_timestamp,
    "find_unclassified_by_semantic_group": find_unclassified_by_semantic_group,
    "store_email_record": store_email_record,
    "process_and_store_email": process_and_store_email,
    "cluster_unclassified_emails": cluster_unclassified_emails,
    "create_label": create_label,
    "get_labels": get_labels,
    "apply_label_to_email": apply_label_to_email,
    "get_emails_by_id": get_emails_by_id
}

# --- 2. MCP INTERFACE ENDPOINTS ---

@app.get("/mcp")
async def mcp_discovery():
    return {
        "mcp_server": "SmartEmailManager",
        "status": "active",
        "available_tools": list(TOOLS.keys())
    }

@app.post("/mcp/call")
async def call_mcp_tool(request: Request):
    try:
        body = await request.json()
        tool_name = body.get("tool")
        arguments = body.get("arguments", {})
        if tool_name not in TOOLS:
            raise HTTPException(status_code=404, detail=f"Tool {tool_name} not found.")
        result = TOOLS[tool_name](**arguments)
        return {"status": "success", "result": result}
    except Exception as e:
        print(f"Tool Execution Error ({tool_name}): {str(e)}")
        return {"status": "error", "message": str(e)}

# --- 3. INFRASTRUCTURE PROVISIONING ---

class MongoSetupRequest(BaseModel):
    mongo_public_key: str
    mongo_private_key: str
    user_email: str
    gmail_token: str

def generate_secure_password(length=16):
    characters = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(characters) for i in range(length))

def enable_gcp_api(project_id: str, service_name: str):
    """Programmatically enables a GCP service."""
    try:
        client = service_usage_v1.ServiceUsageClient()
        # Corrected: Pass the name argument positionally as expected by the client
        operation = client.enable_service(request={"name": f"projects/{project_id}/services/{service_name}"})
        operation.result()
    except Exception as e:
        print(f"API Enablement Warning for {service_name}: {str(e)}")

def setup_agent_playbook(project_id: str, agent_id: str, location: str = "global"):
    client = dialogflow.PlaybooksClient(client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"})
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
    """Provisions Vertex AI Agent Builder resources with idempotency check."""
    enable_gcp_api(project_id, "discoveryengine.googleapis.com")
    enable_gcp_api(project_id, "dialogflow.googleapis.com")
    enable_gcp_api(project_id, "aiplatform.googleapis.com")

    parent = f"projects/{project_id}/locations/{location}/collections/default_collection"
    
    # 1. Handle Data Store (Idempotent)
    ds_client = discoveryengine.DataStoreServiceClient()
    ds_id = None
    
    # Check if a data store already exists
    existing_ds = ds_client.list_data_stores(parent=parent)
    for ds in existing_ds:
        if ds.display_name == "Email Knowledge Base":
            ds_id = ds.name.split("/")[-1]
            print(f"Found existing Data Store: {ds_id}")
            break
            
    if not ds_id:
        ds_id = f"email-ds-{uuid.uuid4().hex[:6]}"
        data_store_dict = {
            "display_name": "Email Knowledge Base",
            "industry_vertical": "GENERIC",
            "content_config": "CONTENT_REQUIRED",
        }
        ds_operation = ds_client.create_data_store(parent=parent, data_store=data_store_dict, data_store_id=ds_id)
        ds_operation.result()
        print(f"Created new Data Store: {ds_id}")

    # 2. Handle Engine (Idempotent)
    engine_client = discoveryengine.EngineServiceClient()
    engine_resource_id = None
    
    # Check if an engine already exists
    existing_engines = engine_client.list_engines(parent=parent)
    for eng in existing_engines:
        if eng.display_name == "Smart Email Manager":
            engine_resource_id = eng.name.split("/")[-1]
            print(f"Found existing Engine: {engine_resource_id}")
            break

    # fallback: Check Dialogflow Agents if engine not found directly
    if not engine_resource_id:
        try:
            df_client = dialogflow.AgentsClient(client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"})
            df_parent = f"projects/{project_id}/locations/{location}"
            for agent in df_client.list_agents(parent=df_parent):
                if agent.display_name == "Smart Email Manager":
                    if agent.gen_app_builder_settings and agent.gen_app_builder_settings.engine:
                        engine_resource_id = agent.gen_app_builder_settings.engine.split("/")[-1]
                        print(f"Recovered Engine ID from Dialogflow Agent: {engine_resource_id}")
                        break
        except Exception as df_e:
            print(f"Dialogflow Agent Lookup Note: {str(df_e)}")

    if not engine_resource_id:
        engine_resource_id = f"email-agent-{uuid.uuid4().hex[:6]}"
        engine_dict = {
            "display_name": "Smart Email Manager",
            "solution_type": "SOLUTION_TYPE_CHAT",
            "data_store_ids": [ds_id],
            "industry_vertical": "GENERIC",
            "chat_engine_config": {
                "agent_creation_config": {
                    "business": "Smart Email Manager",
                    "default_language_code": "en",
                    "time_zone": "UTC"
                }
            }
        }
        try:
            engine_operation = engine_client.create_engine(parent=parent, engine=engine_dict, engine_id=engine_resource_id)
            engine_operation.result()
            print(f"Created new Engine: {engine_resource_id}")
        except Exception as e:
            if "AlreadyExists" in str(e) or "409" in str(e):
                print(f"Engine or Agent already exists, attempting to recover...")
                existing_engines = engine_client.list_engines(parent=parent)
                for eng in existing_engines:
                    if eng.display_name == "Smart Email Manager":
                        engine_resource_id = eng.name.split("/")[-1]
                        print(f"Recovered existing Engine: {engine_resource_id}")
                        break
                if not engine_resource_id:
                    raise e
            else:
                raise e

    try:
        # Playbooks can be multiple, we try to create a fresh one or handle failure
        playbook_name = setup_agent_playbook(project_id, engine_resource_id, location)
    except Exception as e:
        print(f"Playbook Note (Already exists?): {str(e)}")
        playbook_name = "existing-or-manual"

    return {"data_store_id": ds_id, "engine_id": engine_resource_id, "playbook_name": playbook_name}

def setup_pubsub(project_id: str):
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic_id = "gmail-notifications"
    topic_path = publisher.topic_path(project_id, topic_id)
    try:
        publisher.create_topic(name=topic_path)
    except Exception:
        pass

    policy = publisher.get_iam_policy(request={"resource": topic_path})
    gmail_sa = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
    if not any(gmail_sa in b.members for b in policy.bindings if b.role == "roles/pubsub.publisher"):
        publisher.set_iam_policy(request={
            "resource": topic_path,
            "policy": {"bindings": [{"role": "roles/pubsub.publisher", "members": [gmail_sa]}]}
        })

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

@app.post("/api/setup-db")
async def setup_infrastructure(setup_request: MongoSetupRequest):
    public_key = setup_request.mongo_public_key
    private_key = setup_request.mongo_private_key
    user_email = setup_request.user_email
    project_id = os.environ.get("PROJECT_ID", "grah-2026") 
    auth = HTTPDigestAuth(public_key, private_key)
    headers = {"Accept": "application/vnd.atlas.2023-01-01+json", "Content-Type": "application/json"}
    try:
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
        agent_builder_info = setup_agent_builder(project_id)
        pubsub_topic = setup_pubsub(project_id)
        client = get_client()
        db = client["smart_email_manager"]
        db["UserSessions"].update_one(
            {"user_email": user_email},
            {
                "$set": {
                    "status": "ready",
                    "mongo_project_id": mongo_project_id,
                    "agent_builder": agent_builder_info,
                    "credentials": {"access_token": setup_request.gmail_token},
                    "updated_at": datetime.now(UTC).isoformat()
                }
            },
            upsert=True
        )
        return {
            "status": "In-Progress", "message": "Setup successful.",
            "mongo_project_id": mongo_project_id, "agent_builder_app": agent_builder_info["engine_id"],
            "pubsub_topic": pubsub_topic, "mcp_url": f"{os.environ.get('CLOUD_RUN_URL', 'https://agent.run.app')}/mcp/call"
        }
    except Exception as e:
        print(f"CRITICAL SETUP ERROR: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Setup failed: {str(e)}")

@app.post("/api/on-new-mail")
async def handle_new_mail(request: Request):
    try:
        envelope = await request.json()
        import base64
        pubsub_message = envelope.get("message", {})
        data_str = base64.b64decode(pubsub_message.get("data", "")).decode("utf-8")
        notification = json.loads(data_str)
        user_email = notification.get("emailAddress")
        new_history_id = notification.get("historyId")
        client = get_client()
        db = client["smart_email_manager"]
        user_session = db["UserSessions"].find_one({"user_email": user_email})
        if not user_session: return {"status": "ignored"}
        last_history_id = user_session.get("last_history_id")
        from tools.gmail_mcp import get_gmail_service
        gmail = get_gmail_service(user_email)
        if not last_history_id:
            db["UserSessions"].update_one({"user_email": user_email}, {"$set": {"last_history_id": new_history_id}})
            return {"status": "initialized"}
        history_res = gmail.users().history().list(userId="me", startHistoryId=last_history_id, historyTypes=["messageAdded"]).execute()
        for change in history_res.get("history", []):
            for item in change.get("messagesAdded", []):
                msg_id = item.get("message", {}).get("id")
                msg_detail = gmail.users().messages().get(userId="me", id=msg_id).execute()
                metadata = {"subject": "New Mail", "message_id": msg_id, "user_email": user_email}
                process_and_store_email(metadata, msg_detail.get("snippet", ""))
        db["UserSessions"].update_one({"user_email": user_email}, {"$set": {"last_history_id": new_history_id}})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error"}

@app.get("/")
async def root():
    return {"message": "Smart Email Manager Agent is running.", "status": "active"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
