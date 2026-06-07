import os
import requests
from requests.auth import HTTPDigestAuth
import json
import uuid
import string
import random
import traceback
from fastapi import FastAPI, Request, HTTPException
from db.mongo_client import get_client, get_collection
from config import settings
from tools.mongo_mcp import get_last_sync_timestamp, setup_database, find_unclassified_by_semantic_group, store_email_record, update_email_lifecycle
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id, remove_label_from_email, send_email
from typing import List, Optional
from pydantic import BaseModel
from google.cloud import discoveryengine_v1beta as discoveryengine
from google.cloud import pubsub_v1
from datetime import datetime, UTC
from toolbox import (
    process_and_store_email, 
    cluster_unclassified_emails, 
    demolish_weak_labels, 
    incremental_update_label_integrity,
    perform_batch_classification,
    get_user_settings
)

# --- 1. INITIALIZE FASTAPI ---
app = FastAPI(title="Smart Email Manager API")

# --- PROXY UTILITIES FOR MULTI-AGENT ORCHESTRATION ---

def proxy_call_sam(method: str, params: dict):
    """Delegates a tool call to the Self-Arizeing Manager (SAM)."""
    sam_url = os.environ.get("SAM_URL")
    if not sam_url:
        # Try to derive it if they share the same project and region
        # e.g. https://smart-email-manager-agent-XYZ.a.run.app -> https://self-arizeing-manager-agent-XYZ.a.run.app
        current_url = os.environ.get("CLOUD_RUN_URL", "")
        if "smart-email-manager-agent" in current_url:
            sam_url = current_url.replace("smart-email-manager-agent", "self-arizeing-manager-agent")
        else:
            return "Error: SAM_URL not configured for orchestration."

    try:
        resp = requests.post(
            f"{sam_url}/mcp/call",
            json={"tool": method, "arguments": params},
            timeout=60 # Extended timeout for cold starts and LLM generation
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("result") if data.get("status") == "success" else data.get("message")
        return f"Error: SAM Agent returned {resp.status_code}"
    except Exception as e:
        return f"Proxy Error: {str(e)}"

def report_hitl_action_proxy(user_id: str, message_id: str, action: str, reason: str = ""):
    return proxy_call_sam("report_hitl_action", {"user_id": user_id, "message_id": message_id, "action": action, "reason": reason})

def generate_weekly_summary_proxy(user_id: str):
    return proxy_call_sam("generate_weekly_summary", {"user_id": user_id})

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
    "remove_label_from_email": remove_label_from_email,
    "get_emails_by_id": get_emails_by_id,
    "demolish_weak_labels": demolish_weak_labels,
    "incremental_update_label_integrity": incremental_update_label_integrity,
    "perform_batch_classification": perform_batch_classification,
    "update_email_lifecycle": update_email_lifecycle,
    "send_email": send_email,
    "report_hitl_action": report_hitl_action_proxy,
    "generate_weekly_summary": generate_weekly_summary_proxy
}


# --- 2. MCP INTERFACE ENDPOINTS ---

@app.get("/mcp")
async def mcp_discovery():
    return {
        "mcp_server": "SmartEmailManager",
        "status": "active",
        "available_tools": list(TOOLS.keys())
    }

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks

# ... existing imports ...

@app.post("/mcp/call")
async def call_mcp_tool(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        tool_name = body.get("tool")
        arguments = body.get("arguments", {})
        
        if tool_name not in TOOLS:
            raise HTTPException(status_code=404, detail=f"Tool {tool_name} not found.")
        
        # --- ASYNC FLOW FOR LONG-RUNNING WEEKLY REPORT ---
        if tool_name == "generate_weekly_summary":
            user_email = arguments.get("user_id")
            
            def run_async_report(email):
                report_content = generate_weekly_summary_proxy(email)
                subject = "[AGENT REPORT] Weekly Inbox Intelligence"
                send_email(user_email=email, to=email, subject=subject, body=report_content)
            
            background_tasks.add_task(run_async_report, user_email)
            
            return {
                "status": "success", 
                "result": "Insight generation started. You will receive an email summary shortly. Please refresh your inbox in a minute."
            }

        # Standard tool execution for others
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
        result = demolish_weak_labels(user_email)
        return {"status": "success", "result": result}
        
    except Exception as e:
        print(f"REORG TRIGGER ERROR: {str(e)}")
        return {"status": "error", "message": str(e)}

@app.post("/api/on-new-mail")
async def handle_new_mail(request: Request, background_tasks: BackgroundTasks):
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

        # Load dynamic settings
        from toolbox import get_user_settings
        config = get_user_settings(user_email)
        
        if not config.get("AUTO_SYNC_NEW_EMAILS", True):
            print(f"[*] AUTO-SYNC DISABLED for {user_email}. Skipping notification processing.")
            return {"status": "skipped", "message": "Auto-sync disabled in settings."}

        # Check MONGO_URI
        if "agent.run.app" in settings.MONGO_URI or "localhost" in settings.MONGO_URI:
            print("CRITICAL ERROR: MONGO_URI is still pointing to a placeholder!")
            return {"status": "error", "message": "Invalid MONGO_URI"}

        client = get_client()
        db = client["smart_email_manager"]
        email_collection = db["EmailData"]
        
        user_session = db["UserSessions"].find_one({"user_email": user_email})
        if not user_session:
            print(f"SESSION NOT FOUND for {user_email}. Auto-registering baseline.")
            db["UserSessions"].update_one(
                {"user_email": user_email}, 
                {"$set": {"last_history_id": new_history_id, "updated_at": datetime.now(UTC).isoformat()}}, 
                upsert=True
            )
            # Background the initial demolish phase
            background_tasks.add_task(demolish_weak_labels, user_email)
            return {"status": "initialized_and_reorganizing_in_background"}

        last_history_id = user_session.get("last_history_id")
        from tools.gmail_mcp import get_gmail_service
        gmail = get_gmail_service(user_email)
        
        if not last_history_id:
            print(f"UPDATING BASELINE HISTORY for {user_email}")
            db["UserSessions"].update_one({"user_email": user_email}, {"$set": {"last_history_id": new_history_id}})
            background_tasks.add_task(demolish_weak_labels, user_email)
            return {"status": "initialized_and_reorganizing_in_background"}

        try:
            history_res = gmail.users().history().list(userId="me", startHistoryId=last_history_id, historyTypes=["messageAdded"]).execute()
            # COMMIT PROGRESS EARLY: Save the new history ID immediately to prevent loops on timeout
            db["UserSessions"].update_one({"user_email": user_email}, {"$set": {"last_history_id": new_history_id, "updated_at": datetime.now(UTC).isoformat()}})
            print(f"[*] History baseline updated to {new_history_id}. Processing batch...")
        except Exception as auth_err:
            if "expired" in str(auth_err).lower() or "401" in str(auth_err):
                return {"status": "error", "message": "token_expired"}
            raise auth_err
        
        processed_count = 0
        batch_freq = config.get("BATCH_CLASSIFICATION_FREQUENCY", 10)
        
        # Process the messages (The long loop)
        for change in history_res.get("history", []):
            for item in change.get("messagesAdded", []):
                msg_id = item.get("message", {}).get("id")
                try:
                    msg_detail = gmail.users().messages().get(userId="me", id=msg_id).execute()
                    
                    # Extract new fields for analytics
                    thread_id = msg_detail.get("threadId")
                    # internalDate is in milliseconds, convert to ISO for BigQuery
                    sent_at_ms = int(msg_detail.get("internalDate", 0))
                    sent_at_iso = datetime.fromtimestamp(sent_at_ms / 1000.0, UTC).isoformat()

                    metadata = {
                        "subject": msg_detail.get("snippet", "New Mail"), 
                        "message_id": msg_id, 
                        "thread_id": thread_id,
                        "sent_at": sent_at_iso,
                        "user_email": user_email
                    }
                    process_and_store_email(metadata, msg_detail.get("snippet", ""))
                    processed_count += 1
                    
                    # Track 3: Stable Classification - Check frequently to clear backlog
                    if processed_count % batch_freq == 0:
                         # Run batch classification in background
                         background_tasks.add_task(perform_batch_classification, user_email)

                except Exception as msg_err:
                    if "404" in str(msg_err):
                        continue
                    else:
                        print(f"Error processing message {msg_id}: {str(msg_err)}")
                        continue

        # Final cleanup classification and stable reorg check in background
        background_tasks.add_task(perform_batch_classification, user_email)
        background_tasks.add_task(demolish_weak_labels, user_email)
        
        return {"status": "success", "processed": processed_count}
    except Exception as e:
        print(f"ON-NEW-MAIL CRASH: {str(e)}")
        return {"status": "error", "detail": str(e)}

@app.post("/api/sync-credentials")
async def sync_credentials(request: Request):
    """
    Endpoint to receive full persistent OAuth credentials from Cloud Shell.
    """
    try:
        data = await request.json()
        user_email = data.get("user_email")
        creds = data.get("credentials")
        
        if not user_email or not creds:
            raise HTTPException(status_code=400, detail="user_email and credentials are required.")

        client = get_client()
        db = client["smart_email_manager"]
        
        db["UserSessions"].update_one(
            {"user_email": user_email},
            {"$set": {
                "credentials": creds,
                "updated_at": datetime.now(UTC).isoformat()
            }},
            upsert=True
        )
        
        return {"status": "success", "message": "Persistent credentials synchronized!"}
    except Exception as e:
        print(f"Sync Error: {str(e)}")
        return {"status": "error", "message": str(e)}

@app.get("/api/settings")
async def get_settings(user_email: str):
    """Retrieves user-specific agent settings."""
    try:
        return get_user_settings(user_email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/settings")
async def update_settings(request: Request):
    """Updates user-specific agent settings in MongoDB with partial merge support."""
    try:
        data = await request.json()
        user_email = data.get("user_email")
        new_settings = data.get("settings")
        
        if not user_email or new_settings is None:
            raise HTTPException(status_code=400, detail="user_email and settings are required.")
            
        client = get_client()
        db = client["smart_email_manager"]
        
        # Construct MongoDB dot notation for merging partial updates
        update_fields = {"updated_at": datetime.now(UTC).isoformat()}
        if isinstance(new_settings, dict):
            for key, value in new_settings.items():
                update_fields[f"agent_settings.{key}"] = value
                
        db["UserSessions"].update_one(
            {"user_email": user_email},
            {"$set": update_fields},
            upsert=True
        )
        return {"status": "success", "message": "Settings updated successfully."}
    except Exception as e:
        print(f"Settings Update Error: {str(e)}")
        return {"status": "error", "message": str(e)}

@app.post("/api/sync-historical")
async def sync_historical(request: Request):
    """
    Triggers a manual sync of emails for a specific time range.
    Supports 'lookback_days' or 'lookback_minutes'.
    """
    try:
        data = await request.json()
        user_email = data.get("user_email")
        lookback_days = data.get("lookback_days")
        lookback_minutes = data.get("lookback_minutes")
        
        if lookback_minutes:
            print(f"[*] QUICK SYNC STARTING for {user_email} (Lookback: {lookback_minutes} minutes)")
            from toolbox import sync_by_time_range
            processed = sync_by_time_range(user_email, minutes=lookback_minutes)
            msg = f"Quick sync for last {lookback_minutes} minutes complete. Processed {processed} emails."
        else:
            days = lookback_days or 30
            print(f"[*] MANUAL HISTORICAL SYNC STARTING for {user_email} (Lookback: {days} days)")
            # Standard historical sync logic (placeholder for full background job)
            msg = f"Historical sync for last {days} days triggered!"
        
        # Trigger an immediate reorg and batch classification to clear backlog
        demolish_weak_labels(user_email)
        perform_batch_classification(user_email)
        
        return {"status": "success", "message": msg}
    except Exception as e:
        print(f"Historical Sync Error: {str(e)}")
        return {"status": "error", "message": str(e)}

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

    try:
        client = get_client()
        client.admin.command('ping', serverSelectionTimeoutMS=2000)
        db = client["smart_email_manager"]
        results["database"] = {"status": "ok", "message": "Connected to MongoDB Atlas."}
    except Exception as e:
        results["database"] = {"status": "error", "message": f"DB Connection Failed: {str(e)}"}

    try:
        publisher = pubsub_v1.PublisherClient()
        subscriber = pubsub_v1.SubscriberClient()
        topic_path = publisher.topic_path(project_id, "gmail-notifications")
        sub_path = subscriber.subscription_path(project_id, "gmail-notifications-sub")
        
        try:
            publisher.get_topic(topic=topic_path, timeout=3)
            topic_ok = True
        except Exception: topic_ok = False
        
        try:
            sub = subscriber.get_subscription(subscription=sub_path, timeout=3)
            endpoint = sub.push_config.push_endpoint
            sub_ok = "ok" if "agent.run.app" not in endpoint else "warning"
        except Exception: sub_ok = "error"
        
        results["pubsub"] = {"status": "ok" if (topic_ok and sub_ok == "ok") else sub_ok}
    except Exception as e:
        results["pubsub"] = {"status": "error", "message": "Pub/Sub check timed out"}

    try:
        client = discoveryengine.EngineServiceClient()
        parent = f"projects/{project_id}/locations/global/collections/default_collection"
        engines = client.list_engines(parent=parent, timeout=5)
        found = any(e.display_name == "Smart Email Manager" for e in engines)
        results["vertex_ai"] = {"status": "ok" if found else "error"}
    except Exception as e:
        results["vertex_ai"] = {"status": "error", "message": "Vertex AI check timed out"}

    try:
        headers = {"Authorization": f"Bearer {gmail_token}"}
        resp = requests.head("https://gmail.googleapis.com/gmail/v1/users/me/profile", headers=headers, timeout=3)
        results["gmail_watch"] = {"status": "ok" if resp.status_code == 200 else "error"}
    except Exception:
        results["gmail_watch"] = {"status": "error", "message": "Gmail API unreachable"}

    return results

@app.post("/api/check-connection")
async def check_connection(gmail_token: str, project_id: str):
    return await verify_system(gmail_token, project_id)

@app.get("/")
async def root():
    return {"message": "Smart Email Manager Agent is active.", "status": "active"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
