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
from google.cloud import pubsub_v1
from datetime import datetime, UTC
from toolbox import (
    process_and_store_email, 
    cluster_unclassified_emails, 
    reorganize_mails, 
    incremental_update_label_integrity
)

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
    "get_emails_by_id": get_emails_by_id,
    "reorganize_mails": reorganize_mails,
    "incremental_update_label_integrity": incremental_update_label_integrity
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

# --- 3. CORE SERVICE ENDPOINTS ---

@app.post("/api/reorganize")
async def trigger_reorganize(request: Request):
    """
    Subscriber endpoint for the 'reorganize-inbox' Pub/Sub topic.
    """
    try:
        envelope = await request.json()
        import base64
        pubsub_message = envelope.get("message", {})
        data_str = base64.b64decode(pubsub_message.get("data", "")).decode("utf-8")
        event = json.loads(data_str)
        
        user_email = event.get("user_email")
        reason = event.get("reason")
        
        print(f"REORG EVENT RECEIVED for {user_email} (Reason: {reason})")
        
        # Execute the orchestrator
        result = reorganize_mails(user_email)
        return {"status": "success", "result": result}
        
    except Exception as e:
        print(f"REORG TRIGGER ERROR: {str(e)}")
        return {"status": "error", "message": str(e)}

@app.post("/api/on-new-mail")
async def handle_new_mail(request: Request):
    """
    Web-hook triggered by Pub/Sub when a new Gmail notification arrives.
    """
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
        
        # Ensure session exists
        user_session = db["UserSessions"].find_one({"user_email": user_email})
        if not user_session:
            print(f"SESSION NOT FOUND for {user_email}. Auto-registering history ID.")
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
                print(f"CRITICAL: Gmail Token for {user_email} has expired. Refresh needed.")
                return {"status": "error", "message": "token_expired"}
            raise auth_err
        
        processed_count = 0
        for change in history_res.get("history", []):
            for item in change.get("messagesAdded", []):
                msg_id = item.get("message", {}).get("id")
                msg_detail = gmail.users().messages().get(userId="me", id=msg_id).execute()
                metadata = {
                    "subject": msg_detail.get("snippet", "New Mail"), 
                    "message_id": msg_id, 
                    "user_email": user_email
                }
                process_and_store_email(metadata, msg_detail.get("snippet", ""))
                processed_count += 1
        
        # After processing, trigger reorg check (e.g., if no sem_ labels exist yet)
        reorganize_mails(user_email)
        
        print(f"SUCCESS: Processed {processed_count} emails.")
        db["UserSessions"].update_one({"user_email": user_email}, {"$set": {"last_history_id": new_history_id}})
        return {"status": "success", "processed": processed_count}
    except Exception as e:
        print(f"ON-NEW-MAIL CRASH: {str(e)}")
        print(traceback.format_exc())
        return {"status": "error", "detail": str(e)}

@app.get("/api/verify-system")
async def verify_system(gmail_token: str, project_id: str):
    """
    Comprehensive diagnostic of the entire Agent infrastructure.
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
        
        headers = {"Authorization": f"Bearer {gmail_token}"}
        profile_res = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers)
        if profile_res.status_code == 200:
            user_email = profile_res.json().get("emailAddress")
            user_session = db["UserSessions"].find_one({"user_email": user_email})
            
            # Auto-Heal: If user exists but token is missing, update it
            db["UserSessions"].update_one(
                {"user_email": user_email},
                {"$set": {"credentials": {"access_token": gmail_token}, "updated_at": datetime.now(UTC).isoformat()}},
                upsert=True
            )
            
            results["database"] = {"status": "ok", "message": f"Connected. User {user_email} synchronized."}
        else:
            results["database"] = {"status": "warning", "message": "Connected, but Gmail token is invalid."}
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
                sub_msg = "Subscription uses placeholder URL."
            else:
                sub_ok = "ok"
                sub_msg = "Subscription linked."
        except Exception: 
            sub_ok = "error"
            sub_msg = "Subscription missing."

        if topic_ok and sub_ok == "ok":
            results["pubsub"] = {"status": "ok", "message": "Healthy."}
        else:
            results["pubsub"] = {"status": sub_ok, "message": sub_msg if not topic_ok else f"Topic OK. {sub_msg}"}
    except Exception as e:
        results["pubsub"] = {"status": "error", "message": str(e)}

    # 3. Check Vertex AI (Agent Builder)
    try:
        client = discoveryengine.EngineServiceClient()
        parent = f"projects/{project_id}/locations/global/collections/default_collection"
        engines = client.list_engines(parent=parent)
        found = any(e.display_name == "Smart Email Manager" for e in engines)
        results["vertex_ai"] = {"status": "ok" if found else "error", "message": "Engine found" if found else "Engine missing"}
    except Exception as e:
        results["vertex_ai"] = {"status": "error", "message": str(e)}

    # 4. Check Gmail Watch
    try:
        headers = {"Authorization": f"Bearer {gmail_token}"}
        resp = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers)
        results["gmail_watch"] = {"status": "ok" if resp.status_code == 200 else "error", "message": "Accessible" if resp.status_code == 200 else "Denied"}
    except Exception as e:
        results["gmail_watch"] = {"status": "error", "message": str(e)}

    return results

@app.post("/api/check-connection")
async def check_connection(gmail_token: str, project_id: str):
    """Legacy redirect for the Sidebar UI during transition."""
    return await verify_system(gmail_token, project_id)

@app.get("/")
async def root():
    return {"message": "Smart Email Manager Agent is active.", "status": "active"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
