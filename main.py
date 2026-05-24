from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
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
    """Returns the MongoDB connection status for a user."""
    if user_email in user_db and user_db[user_email].get("connected"):
        return {"status": "connected", "logged_in": True}
    return {"status": "disconnected", "logged_in": False}

@app.get("/mongodb/login")
async def mongodb_login(user_email: str):
    """Initiates the MongoDB OAuth flow."""
    auth_url = logintoMongoDB_OAuth(user_email)
    return {"auth_url": auth_url}

@app.get("/callback")
async def oauth_callback(state: str, code: str = "placeholder_code"):
    """Callback for the OAuth flow."""
    user_email = state
    # Mark user as connected
    user_db[user_email] = {
        "connected": True,
        "tokens": {"access_token": "fake_token_" + code}
    }
    return {"message": f"Successfully authenticated for {user_email}. You can now close this tab and return to the Gmail Sidebar."}

@app.get("/")
async def root():
    return {"message": "Smart Email Manager Agent is running."}

if __name__ == "__main__":
    import uvicorn
    # Use port 8080 as standard for Cloud Run
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
