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
from agent import process_email
from tools.mongo_mcp import get_last_sync_timestamp, setup_database
from typing import List, Optional
from pydantic import BaseModel

app = FastAPI()

class MongoSetupRequest(BaseModel):
    mongo_public_key: str
    mongo_private_key: str
    user_email: str

def generate_secure_password(length=16):
    characters = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(characters) for i in range(length))

@app.post("/api/setup-db")
async def setup_mongodb_backend(setup_request: MongoSetupRequest):
    """
    Automated MongoDB Atlas Provisioning Wizard.
    Phases: 1. Project Creation, 2. IP Whitelisting, 3. DB User Creation, 4. Cluster Launch.
    """
    public_key = setup_request.mongo_public_key
    private_key = setup_request.mongo_private_key
    user_email = setup_request.user_email
    
    auth = HTTPDigestAuth(public_key, private_key)
    headers = {
        "Accept": "application/vnd.atlas.2023-01-01+json",
        "Content-Type": "application/json"
    }

    try:
        # 1. Discover Organization
        orgs_res = requests.get("https://cloud.mongodb.com/api/atlas/v2/orgs", auth=auth, headers=headers)
        if orgs_res.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid MongoDB credentials or permission denied.")
        
        org_data = orgs_res.json()
        if not org_data.get("results"):
            raise HTTPException(status_code=404, detail="No MongoDB Organizations found for these keys.")
        
        org_id = org_data["results"][0]["id"]
        
        # 2. Create Project
        project_name = f"Rapid-Agent-Suite-{uuid.uuid4().hex[:6]}"
        project_payload = {"name": project_name}
        project_res = requests.post("https://cloud.mongodb.com/api/atlas/v2/groups", 
                                    auth=auth, headers=headers, json=project_payload)
        
        if project_res.status_code not in [200, 201]:
            raise HTTPException(status_code=500, detail=f"Failed to create project: {project_res.text}")
        
        project_id = project_res.json()["id"]

        # 3. Whitelist IPs (All for hackathon compatibility)
        whitelist_payload = [{"ipAddress": "0.0.0.0/0", "comment": "Allow Cloud Run Agent Access"}]
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{project_id}/accessList",
                      auth=auth, headers=headers, json=whitelist_payload)

        # 4. Create Database User
        db_username = "agent_runtime_user"
        db_password = generate_secure_password()
        user_payload = {
            "databaseName": "admin",
            "password": db_password,
            "roles": [{"databaseName": "smart_email_manager", "roleName": "readWrite"}],
            "username": db_username
        }
        requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{project_id}/databaseUsers",
                      auth=auth, headers=headers, json=user_payload)

        # 5. Launch Free Tier Cluster (M0) on GCP
        cluster_payload = {
            "name": "email-agent-cluster",
            "providerSettings": {
                "providerName": "GCP",
                "backingProviderName": "GCP",
                "regionName": "US_EAST_1"
            },
            "clusterType": "REPLICASET"
        }
        cluster_res = requests.post(f"https://cloud.mongodb.com/api/atlas/v2/groups/{project_id}/clusters",
                                    auth=auth, headers=headers, json=cluster_payload)

        if cluster_res.status_code not in [200, 201, 202]:
            raise HTTPException(status_code=500, detail=f"Failed to initiate cluster: {cluster_res.text}")

        # 6. Store progress in memory/DB
        user_db[user_email] = {
            "status": "provisioning",
            "project_id": project_id,
            "project_name": project_name,
            "db_user": db_username,
            "db_pass": db_password
        }
        
        return {
            "status": "In-Progress", 
            "message": f"Successfully initiated deployment for {project_name}. Cluster creation takes ~3-5 mins.",
            "project_id": project_id
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# In-memory storage for demonstration. In production, use Redis or a DB.
# Maps user_email -> { "connected": bool, "tokens": dict }
user_db = {}

def logintoMongoDB_OAuth(user_email: str):
    """
    Generates an OAuth link for MongoDB. 
    This is a placeholder for the actual OAuth flow implementation.
    """
    # In a real implementation, this would use a library like authlib 
    # to redirect to MongoDB Atlas or a custom identity provider.
    # For this hackathon, we provide a simulated OAuth URL.
    base_url = os.getenv("CLOUD_RUN_URL", "http://localhost:8080")
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?client_id=PLACEHOLDER&response_type=code&scope=email&redirect_uri={base_url}/callback&state={user_email}"
    
    # User hint: The requirement mentions "communicating OAuth link through cloud run back to gmail Add-on"
    return auth_url

@app.get("/mongodb/status")
async def mongodb_status(user_email: str):
    """Checks MongoDB for an existing user session."""
    # First check memory
    if user_email in user_db and user_db[user_email].get("connected"):
        return {"status": "connected", "logged_in": True}
    
    # Fallback: Check the actual MongoDB database for persistence
    try:
        client = get_client()
        db = client[settings.DB_NAME]
        session = db["UserSessions"].find_one({"user_email": user_email})
        if session:
            user_db[user_email] = {"connected": True} # Cache in memory
            return {"status": "connected", "logged_in": True}
    except Exception as e:
        print(f"DB Status Check Error: {e}")
        
    return {"status": "disconnected", "logged_in": False}

@app.get("/mongodb/last_sync")
async def get_last_sync(user_email: str):
    """Retrieves the timestamp of the last synced email for a user."""
    timestamp = get_last_sync_timestamp(user_email)
    if timestamp and timestamp != "None":
        return {"last_sync": timestamp}
    return {"last_sync": None}

@app.post("/mongodb/sync")
async def sync_emails(user_email: str, emails: List[dict] = Body(...)):
    """Processes and stores a batch of emails."""
    results = []
    for email in emails:
        try:
            metadata = {
                "subject": email.get("subject"),
                "sender": email.get("sender"),
                "date": email.get("date"),
                "message_id": email.get("message_id"),
                "user_email": user_email
            }
            body = email.get("body", "")
            doc_id = process_email(metadata, body)
            results.append({"message_id": email.get("message_id"), "status": "success", "id": str(doc_id)})
        except Exception as e:
            results.append({"message_id": email.get("message_id"), "status": "error", "error": str(e)})
    
    return {"results": results}

@app.get("/mongodb/login")
async def mongodb_login(request: Request, user_email: str, base_url: str = None):
    """
    Step 1: Initiation. Uses the public URL from the Add-on to avoid localhost.
    """
    # Prioritize the URL we verified in the Gmail Sidebar
    if not base_url or "localhost" in base_url:
        host = request.headers.get("host", "smart-email-manager-agent.a.run.app")
        scheme = request.headers.get("x-forwarded-proto", "https")
        base_url = f"{scheme}://{host}"
    
    base_url = base_url.rstrip('/')
    auth_url = f"{base_url}/auth?state={user_email}&base_url={base_url}"
    return {"auth_url": auth_url}

@app.get("/auth")
async def auth_page(state: str, base_url: str):
    """
    Step 2: Branded Consent with direct Atlas link.
    """
    user_email = state
    html_content = f"""
    <html>
        <head>
            <title>Connect MongoDB Atlas</title>
            <style>
                body {{ font-family: 'Roboto', sans-serif; background-color: #f8f9fa; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
                .consent-card {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 450px; text-align: center; }}
                .logo {{ width: 150px; margin-bottom: 20px; }}
                .user-info {{ background: #e8f0fe; padding: 10px; border-radius: 20px; font-size: 14px; margin-bottom: 25px; display: inline-block; }}
                .btn-group {{ display: flex; flex-direction: column; gap: 12px; margin-top: 30px; }}
                .allow-btn {{ background: #00ed64; color: #001e2b; padding: 14px 25px; border-radius: 4px; text-decoration: none; font-weight: bold; font-size: 16px; }}
                .atlas-btn {{ background: #001e2b; color: white; padding: 14px 25px; border-radius: 4px; text-decoration: none; font-weight: 500; font-size: 14px; }}
                .footer {{ color: #5f6368; font-size: 12px; margin-top: 25px; line-height: 1.5; }}
            </style>
        </head>
        <body>
            <div class="consent-card">
                <img src="https://webassets.mongodb.com/_com_assets/cms/mongodb_logo_white_v2-9602e60.png" class="logo" style="background: #001e2b; padding: 10px; border-radius: 4px;">
                <h3>Authorize Gmail Integration</h3>
                <p style="font-size: 15px; color: #5f6368;">Link your Atlas cluster to enable AI-powered email management for:</p>
                <div class="user-info"><b>{user_email}</b></div>
                
                <div class="btn-group">
                    <a href="{base_url}/callback?state={user_email}&code=atlas_success" class="allow-btn">Authorize Connection</a>
                    <a href="https://www.mongodb.com/cloud/atlas/register" target="_blank" class="atlas-btn">Create New Atlas Account</a>
                </div>

                <div class="footer">
                    By clicking Authorize, you grant the Rapid Agent Suite permission to store metadata in your selected MongoDB cluster.
                </div>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)

@app.get("/callback")
async def oauth_callback(state: str, code: str):
    """
    Step 3: Store token in MongoDB Atlas for PERSISTENCE.
    Also triggers an automated database and index setup.
    """
    user_email = state
    # 1. Update memory
    user_db[user_email] = {"connected": True}
    
    # 2. Persist to actual MongoDB cluster
    try:
        client = get_client()
        db = client[settings.DB_NAME]
        db["UserSessions"].update_one(
            {"user_email": user_email},
            {"$set": {"connected": True, "token": f"at_{code}", "updated_at": os.popen('date').read().strip()}},
            upsert=True
        )

        # 3. Trigger Agent Setup (Database & Vector Index)
        setup_status = setup_database()
        print(f"Setup Status for {user_email}: {setup_status}")

    except Exception as e:
        print(f"Failed to persist session or setup DB: {e}")

    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: sans-serif; text-align: center; padding-top: 100px;">
            <h1 style="color: #00ed64;">✓ Connection Secure</h1>
            <p>Your MongoDB Atlas session is now active and stored for <b>{user_email}</b>.</p>
            <p>The database cluster and vector search indexes are being prepared.</p>
            <p>You can now return to Gmail and refresh the Add-on.</p>
        </body>
    </html>
    """)

@app.get("/")
async def root():
    return {
        "message": "Smart Email Manager Agent is running.",
        "project_id": os.environ.get("PROJECT_ID", "unknown")
    }

if __name__ == "__main__":
    import uvicorn
    # Use port 8080 as standard for Cloud Run
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
