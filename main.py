from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
import os
from db.mongo_client import get_client
from config import settings

app = FastAPI()

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

@app.get("/mongodb/login")
async def mongodb_login(request: Request, user_email: str, base_url: str = None):
    """
    Step 1: Cloud Run generates a 'real-style' OAuth initiation link.
    """
    if not base_url:
        host = request.headers.get("host", "localhost:8080")
        scheme = request.headers.get("x-forwarded-proto", "https")
        base_url = f"{scheme}://{host}"
    
    base_url = base_url.rstrip('/')
    auth_url = f"{base_url}/auth?state={user_email}"
    return {"auth_url": auth_url}

@app.get("/auth")
async def auth_page(state: str):
    """
    Step 2: Branded User consent page.
    """
    user_email = state
    html_content = f"""
    <html>
        <head>
            <title>Authorize MongoDB Connection</title>
            <style>
                body {{ font-family: 'Roboto', sans-serif; background-color: #f8f9fa; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
                .consent-card {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 400px; text-align: center; }}
                .logo {{ width: 150px; margin-bottom: 20px; }}
                .user-info {{ background: #e8f0fe; padding: 10px; border-radius: 20px; font-size: 14px; margin-bottom: 25px; display: inline-block; }}
                .btn-group {{ display: flex; flex-direction: column; gap: 10px; margin-top: 30px; }}
                .allow-btn {{ background: #00ed64; color: #001e2b; padding: 12px 25px; border-radius: 4px; text-decoration: none; font-weight: bold; }}
                .reg-link {{ color: #5f6368; font-size: 13px; text-decoration: none; margin-top: 15px; }}
            </style>
        </head>
        <body>
            <div class="consent-card">
                <img src="https://webassets.mongodb.com/_com_assets/cms/mongodb_logo_white_v2-9602e60.png" class="logo" style="background: #001e2b; padding: 10px; border-radius: 4px;">
                <h3>Connect to Atlas</h3>
                <p style="font-size: 14px; color: #5f6368;">Allow <b>Rapid Agent Gmail Suite</b> to securely link your email metadata with your MongoDB clusters.</p>
                <div class="user-info">User: <b>{user_email}</b></div>
                <div class="btn-group">
                    <a href="/callback?state={user_email}&code=hck_{user_email[:3]}" class="allow-btn">Authorize Connection</a>
                    <a href="https://www.mongodb.com/cloud/atlas/register" target="_blank" class="reg-link">Don't have an account? Register to Atlas</a>
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
    except Exception as e:
        print(f"Failed to persist session: {e}")

    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: sans-serif; text-align: center; padding-top: 100px;">
            <h1 style="color: #00ed64;">✓ Connection Secure</h1>
            <p>Your MongoDB Atlas session is now active and stored for <b>{user_email}</b>.</p>
            <p>You can now return to Gmail and refresh the Add-on.</p>
        </body>
    </html>
    """)

@app.get("/")
async def root():
    return {"message": "Smart Email Manager Agent is running."}

if __name__ == "__main__":
    import uvicorn
    # Use port 8080 as standard for Cloud Run
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
