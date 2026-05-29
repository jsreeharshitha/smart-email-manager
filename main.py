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
    try:
        client = dialogflow.PlaybooksClient(client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"})
        parent = f"projects/{project_id}/locations/{location}/agents/{agent_id}"
        
        # 1. Check if playbook already exists to prevent duplicates
        try:
            existing_playbooks = client.list_playbooks(parent=parent)
            for p in existing_playbooks:
                if p.display_name == "Smart Email Management Playbook":
                    print(f"PLAYBOOK ALREADY EXISTS: {p.name}")
                    return p.name
        except Exception: pass

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
        print(f"PLAYBOOK CREATED SUCCESSFULLY: {response.name}")
        return response.name
    except Exception as e:
        print(f"PLAYBOOK CREATION FAILED: {str(e)}")
        raise e

def setup_agent_builder(project_id: str, location: str = "global"):
    """Provisions Vertex AI Agent Builder resources with Enterprise features."""
    enable_gcp_api(project_id, "discoveryengine.googleapis.com")
    enable_gcp_api(project_id, "dialogflow.googleapis.com")
    enable_gcp_api(project_id, "aiplatform.googleapis.com")

    parent = f"projects/{project_id}/locations/{location}/collections/default_collection"
    
    # ... (Data Store logic same) ...
    ds_client = discoveryengine.DataStoreServiceClient()
    ds_id = None
    existing_ds = ds_client.list_data_stores(parent=parent)
    for ds in existing_ds:
        if ds.display_name == "Email Knowledge Base":
            ds_id = ds.name.split("/")[-1]
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

    # 2. Handle Engine (Idempotent)
    engine_client = discoveryengine.EngineServiceClient()
    engine_resource_id = None
    existing_engines = engine_client.list_engines(parent=parent)
    for eng in existing_engines:
        if eng.display_name == "Smart Email Manager":
            engine_resource_id = eng.name.split("/")[-1]
            break

    # fallback: Check Dialogflow Agents
    if not engine_resource_id:
        try:
            df_client = dialogflow.AgentsClient(client_options={"api_endpoint": f"{location}-dialogflow.googleapis.com"})
            for agent in df_client.list_agents(parent=f"projects/{project_id}/locations/{location}"):
                if agent.display_name == "Smart Email Manager" and agent.gen_app_builder_settings:
                    engine_resource_id = agent.gen_app_builder_settings.engine.split("/")[-1]
                    break
        except Exception: pass

    if not engine_resource_id:
        engine_resource_id = f"email-agent-{uuid.uuid4().hex[:6]}"
        
        # Use REST API directly to bypass SDK schema issues for searchAddOnSpec
        import google.auth
        import google.auth.transport.requests
        from google.oauth2 import service_account
        
        creds, project = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        
        url = f"https://discoveryengine.googleapis.com/v1beta/projects/{project_id}/locations/{location}/collections/default_collection/engines?engineId={engine_resource_id}"
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "displayName": "Smart Email Manager",
            "solutionType": "SOLUTION_TYPE_CHAT",
            "industryVertical": "GENERIC",
            "dataStoreIds": [ds_id],
            "chatEngineConfig": {
                "agentCreationConfig": {
                    "business": "Smart Email Manager",
                    "default_language_code": "en",
                    "time_zone": "UTC"
                }
            }
        }

        
        try:
            resp = requests.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                # Fallback to recovery if it already exists despite our check
                if "AlreadyExists" in resp.text or "409" in str(resp.status_code):
                    print("Engine already exists (REST), recovering...")
                    existing_engines = engine_client.list_engines(parent=parent)
                    for eng in existing_engines:
                        if eng.display_name == "Smart Email Manager":
                            engine_resource_id = eng.name.split("/")[-1]
                            break
                else:
                    print(f"REST Create Engine Error: {resp.text}")
                    raise Exception(f"Failed to create engine: {resp.text}")
            else:
                print(f"Created new Engine (REST): {engine_resource_id}")
        except Exception as e:
            if "AlreadyExists" not in str(e): raise e

    try:
        playbook_name = setup_agent_playbook(project_id, engine_resource_id, location)
    except Exception: playbook_name = "existing-or-manual"

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

@app.post("/api/setup-db-init")
async def setup_db_init(setup_request: MongoSetupRequest):
    """Phase 1: Just trigger MongoDB Atlas provisioning and exit fast."""
    public_key = setup_request.mongo_public_key
    private_key = setup_request.mongo_private_key
    user_email = setup_request.user_email
    
    auth = HTTPDigestAuth(public_key, private_key)
    headers = {"Accept": "application/vnd.atlas.2023-01-01+json", "Content-Type": "application/json"}
    
    try:
        # Quick Atlas API Calls (Takes 1-2 seconds total)
        orgs_res = requests.get("https://cloud.mongodb.com/api/atlas/v2/orgs", auth=auth, headers=headers)
        org_id = orgs_res.json()["results"][0]["id"]
        
        project_name = f"Rapid-Agent-{uuid.uuid4().hex[:6]}"
        project_res = requests.post("https://cloud.mongodb.com/api/atlas/v2/groups", 
                                    auth=auth, headers=headers, json={"name": project_name, "orgId": org_id})
        mongo_project_id = project_res.json()["id"]
        
        # Configure access and user
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/accessList",
                      auth=auth, headers=headers, json=[{"ipAddress": "0.0.0.0/0"}])
        
        db_pass = generate_secure_password()
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/databaseUsers",
                      auth=auth, headers=headers, json={
                          "databaseName": "admin", "password": db_pass, "username": "agent_user",
                          "roles": [{"databaseName": "smart_email_manager", "roleName": "readWrite"}]
                      })

        # Trigger Cluster Build
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/clusters",
                      auth=auth, headers=headers, json={
                          "name": "email-cluster", "clusterType": "REPLICASET",
                          "providerSettings": {"providerName": "TENANT", "backingProviderName": "GCP", 
                                                "instanceSizeName": "M0", "regionName": "CENTRAL_US"}
                      })
        
        return {
            "status": "db_provisioning",
            "mongo_project_id": mongo_project_id,
            "db_pass": db_pass,
            "message": "Database cluster instantiation triggered."
        }
    except Exception as e:
        print(f"INIT SETUP ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/setup-gcp-infra")
async def setup_gcp_infra(project_id: str, mongo_project_id: str, user_email: str, gmail_token: str):
    """Phase 2: Provision Vertex AI / Agent Builder and Pub/Sub in its own window."""
    try:
        agent_builder_info = setup_agent_builder(project_id)
        pubsub_topic = setup_pubsub(project_id)
        
        # Safe structural session payload caching (Database might still be provisioning)
        try:
            client = get_client()
            db = client["smart_email_manager"]
            db["UserSessions"].update_one(
                {"user_email": user_email},
                {"$set": {
                    "mongo_project_id": mongo_project_id,
                    "agent_builder": agent_builder_info,
                    "credentials": {"access_token": gmail_token},
                    "updated_at": datetime.now(UTC).isoformat()
                }}, upsert=True
            )
        except Exception:
            pass # Suppress if cluster isn't ready to receive it yet
            
        return {
            "status": "gcp_ready",
            "agent_builder_app": agent_builder_info["engine_id"],
            "pubsub_topic": pubsub_topic,
            "message": "Vertex AI Environment and Pub/Sub provisioned successfully."
        }
    except Exception as e:
        print(f"GCP INFRA ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

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
        
        print(f"NOTIFICATION RECEIVED: {user_email} (History: {new_history_id})")

        # Ensure we have a real MONGO_URI
        if "agent.run.app" in settings.MONGO_URI or "localhost" in settings.MONGO_URI:
            print("CRITICAL ERROR: MONGO_URI is still pointing to a placeholder!")
            return {"status": "error", "message": "Invalid MONGO_URI"}

        client = get_client()
        db = client["smart_email_manager"]
        
        # Test connection with timeout
        client.admin.command('ping')
        
        user_session = db["UserSessions"].find_one({"user_email": user_email})
        if not user_session:
            print(f"SESSION NOT FOUND for {user_email}. Registering first history ID.")
            db["UserSessions"].update_one(
                {"user_email": user_email}, 
                {"$set": {"last_history_id": new_history_id, "updated_at": datetime.now(UTC).isoformat()}}, 
                upsert=True
            )
            return {"status": "initialized"}

        last_history_id = user_session.get("last_history_id")
        from tools.gmail_mcp import get_gmail_service
        gmail = get_gmail_service(user_email)
        
        if not last_history_id:
            print(f"UPDATING BASELINE HISTORY for {user_email}")
            db["UserSessions"].update_one({"user_email": user_email}, {"$set": {"last_history_id": new_history_id}})
            return {"status": "initialized"}

        print(f"FETCHING HISTORY since {last_history_id}")
        try:
            history_res = gmail.users().history().list(userId="me", startHistoryId=last_history_id, historyTypes=["messageAdded"]).execute()
        except Exception as auth_err:
            if "expired" in str(auth_err).lower() or "401" in str(auth_err):
                print(f"CRITICAL: Gmail Token for {user_email} has expired. User must click 'Verify & Link Inbox' in Sidebar to refresh.")
                return {"status": "error", "message": "token_expired"}
            raise auth_err
        
        processed_count = 0
        for change in history_res.get("history", []):
            for item in change.get("messagesAdded", []):
                msg_id = item.get("message", {}).get("id")
                msg_detail = gmail.users().messages().get(userId="me", id=msg_id).execute()
                metadata = {"subject": msg_detail.get("snippet", "New Mail"), "message_id": msg_id, "user_email": user_email}
                process_and_store_email(metadata, msg_detail.get("snippet", ""))
                processed_count += 1
        
        print(f"SUCCESS: Processed {processed_count} emails.")
        db["UserSessions"].update_one({"user_email": user_email}, {"$set": {"last_history_id": new_history_id}})
        return {"status": "success", "processed": processed_count}
    except Exception as e:
        print(f"ON-NEW-MAIL CRASH: {str(e)}")
        print(traceback.format_exc())
        return {"status": "error", "detail": str(e)}

@app.get("/api/verify-db")
async def verify_database(mongo_project_id: Optional[str] = None, public_key: Optional[str] = None, private_key: Optional[str] = None):
    # 1. Check if we already have a URI and it works
    if os.environ.get("MONGO_URI"):
        try:
            client = get_client()
            client.admin.command('ping')
            return {"status": "ready", "message": "Database is active and reachable."}
        except Exception: pass

    # 2. If no URI or ping failed, and we have credentials, try to fetch URI from Atlas
    if mongo_project_id and public_key and private_key:
        auth = HTTPDigestAuth(public_key, private_key)
        headers = {"Accept": "application/vnd.atlas.2023-01-01+json"}
        try:
            url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{mongo_project_id}/clusters/email-cluster"
            res = requests.get(url, auth=auth, headers=headers)
            data = res.json()
            
            if data.get("stateName") == "IDLE":
                srv_uri = data["connectionStrings"]["standardSrv"]
                # Note: SRV doesn't include user/pass, we need to inject them
                # Our setup-db-init uses 'agent_user' and the generated pass. 
                # For this prototype, we'll return the URI and let frontend/backend handle the merge.
                return {
                    "status": "ready_to_link", 
                    "srv_uri": srv_uri,
                    "message": "Cluster is ready. URI retrieved."
                }
            else:
                return {"status": "provisioning", "message": f"Cluster state: {data.get('stateName', 'Unknown')}"}
        except Exception as e:
            return {"status": "error", "message": f"Atlas API Error: {str(e)}"}

    return {"status": "provisioning", "message": "Waiting for MONGO_URI environment variable."}

@app.post("/api/update-env")
async def update_env(mongo_uri: str):
    """Fallback to set MONGO_URI in the current process and update settings."""
    os.environ["MONGO_URI"] = mongo_uri
    settings.MONGO_URI = mongo_uri
    print(f"CRITICAL: MONGO_URI updated to {mongo_uri}")
    return {"status": "success", "message": "Environment variable and settings updated."}

@app.post("/api/check-connection")
async def check_connection(gmail_token: str, project_id: str):
    """
    Checks if the backend is correctly linked to the user's Gmail.
    1. Verifies/Creates the Pub/Sub topic.
    2. Validates the Gmail Token identity.
    3. Auto-registers the User Session in MongoDB if missing.
    """
    # 1. Ensure Pub/Sub is ready
    try:
        setup_pubsub(project_id)
        topic_ready = True
    except Exception as e:
        print(f"PubSub Setup Check Error: {str(e)}")
        topic_ready = False

    # 2. Try to perform a minimal Gmail Watch call to test the "Identity"
    topic_name = f"projects/{project_id}/topics/gmail-notifications"
    url = "https://gmail.googleapis.com/gmail/v1/users/me/watch"
    headers = {
        "Authorization": f"Bearer {gmail_token}",
        "Content-Type": "application/json"
    }
    payload = {"topicName": topic_name, "labelIds": ["INBOX"]}

    try:
        resp = requests.post(url, json=payload, headers=headers)
        
        # 3. If Gmail accepts the token, ensure the session is in MongoDB
        if resp.status_code == 200:
            try:
                # Use the user email from the Gmail Profile (safest)
                profile_res = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers)
                user_email = profile_res.json().get("emailAddress")
                
                client = get_client()
                db = client["smart_email_manager"]
                db["UserSessions"].update_one(
                    {"user_email": user_email},
                    {"$set": {
                        "credentials": {"access_token": gmail_token},
                        "last_watch_status": "active",
                        "updated_at": datetime.now(UTC).isoformat()
                    }}, upsert=True
                )
                print(f"SESSION AUTO-HEAL: Registered credentials for {user_email}")
            except Exception as db_err:
                print(f"SESSION AUTO-HEAL FAILED: {str(db_err)}")

            return {
                "status": "connected",
                "topic_ready": topic_ready,
                "watch_active": True,
                "message": "All systems green! Credentials synchronized."
            }
        
        error_data = resp.json()
        error_msg = str(error_data)
        
        # Detect the specific "Project 6833" error
        if "68339103434" in error_msg:
            return {
                "status": "handshake_required",
                "topic_ready": topic_ready,
                "watch_active": False,
                "message": "Cloud Shell Handshake required to link project identities."
            }
        
        return {
            "status": "error",
            "topic_ready": topic_ready,
            "watch_active": False,
            "error_detail": error_data
        }
    except Exception as e:
        return {
            "status": "error",
            "topic_ready": topic_ready,
            "watch_active": False,
            "message": str(e)
        }

@app.post("/api/start-watch")
async def start_gmail_watch(gmail_token: str, project_id: str):
    """Starts the Gmail Watch from the backend to match the resource project identity."""
    topic_name = f"projects/{project_id}/topics/gmail-notifications"
    url = "https://gmail.googleapis.com/gmail/v1/users/me/watch"
    headers = {
        "Authorization": f"Bearer {gmail_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "topicName": topic_name,
        "labelIds": ["INBOX"]
    }
    try:
        resp = requests.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"Backend Watch Error: {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.json())
    except Exception as e:
        print(f"Watch exception: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/verify-system")
async def verify_system(gmail_token: str, project_id: str):
    """
    Comprehensive diagnostic of the entire Agent infrastructure.
    Checks DB, Pub/Sub, Vertex AI, and Gmail Watch.
    """
    results = {
        "database": {"status": "error", "message": "Not tested"},
        "pubsub": {"status": "error", "message": "Not tested"},
        "vertex_ai": {"status": "error", "message": "Not tested"},
        "gmail_watch": {"status": "error", "message": "Not tested"}
    }

    # 1. Check Database
    try:
        client = get_client()
        client.admin.command('ping')
        db = client["smart_email_manager"]
        
        # Check if user email is registered (use profile API to get email)
        headers = {"Authorization": f"Bearer {gmail_token}"}
        profile_res = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers)
        if profile_res.status_code == 200:
            user_email = profile_res.json().get("emailAddress")
            user_session = db["UserSessions"].find_one({"user_email": user_email})
            if user_session:
                results["database"] = {"status": "ok", "message": f"Connected. User {user_email} registered."}
            else:
                results["database"] = {"status": "warning", "message": "Connected, but user not registered. Trigger 'Verify & Link' in Sidebar."}
        else:
            results["database"] = {"status": "warning", "message": "Connected, but could not verify user email (Token issue)."}
    except Exception as e:
        results["database"] = {"status": "error", "message": f"DB Connection Failed: {str(e)}"}

    # 2. Check Pub/Sub
    try:
        publisher = pubsub_v1.PublisherClient()
        subscriber = pubsub_v1.SubscriberClient()
        topic_path = publisher.topic_path(project_id, "gmail-notifications")
        sub_path = subscriber.subscription_path(project_id, "gmail-notifications-sub")
        
        try:
            publisher.get_topic(topic=topic_path)
            topic_ok = True
        except Exception: topic_ok = False
        
        try:
            sub = subscriber.get_subscription(subscription=sub_path)
            endpoint = sub.push_config.push_endpoint
            if "agent.run.app" in endpoint:
                sub_ok = "warning"
                sub_msg = "Subscription exists but uses placeholder URL."
            else:
                sub_ok = "ok"
                sub_msg = "Subscription ready."
        except Exception: 
            sub_ok = "error"
            sub_msg = "Subscription missing."

        if topic_ok and sub_ok == "ok":
            results["pubsub"] = {"status": "ok", "message": "Topic and Subscription are healthy."}
        else:
            results["pubsub"] = {"status": sub_ok, "message": sub_msg if not topic_ok else f"Topic OK. {sub_msg}"}
    except Exception as e:
        results["pubsub"] = {"status": "error", "message": str(e)}

    # 3. Check Vertex AI (Agent Builder)
    try:
        # Simple check: list engines to see if one matches our name
        client = discoveryengine.EngineServiceClient()
        parent = f"projects/{project_id}/locations/global/collections/default_collection"
        engines = client.list_engines(parent=parent)
        found = any(e.display_name == "Smart Email Manager" for e in engines)
        if found:
            results["vertex_ai"] = {"status": "ok", "message": "Agent Engine found."}
        else:
            results["vertex_ai"] = {"status": "error", "message": "Smart Email Manager engine not found."}
    except Exception as e:
        results["vertex_ai"] = {"status": "error", "message": str(e)}

    # 4. Check Gmail Watch
    try:
        headers = {"Authorization": f"Bearer {gmail_token}"}
        # Note: There is no 'get watch' API, but we can check the profile's historyId
        # and see if our backend is receiving notifications. 
        # For now, we'll try a minimal watch call or just check profile.
        resp = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers)
        if resp.status_code == 200:
            results["gmail_watch"] = {"status": "ok", "message": "Gmail API accessible."}
        else:
            results["gmail_watch"] = {"status": "error", "message": "Gmail API access denied (Token expired?)."}
    except Exception as e:
        results["gmail_watch"] = {"status": "error", "message": str(e)}

    return results

@app.get("/")
async def root():
    return {"message": "Smart Email Manager Agent is running.", "status": "active"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
