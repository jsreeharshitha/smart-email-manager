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
    """Returns the MongoDB connection status for a user."""
    if user_email in user_db and user_db[user_email].get("connected"):
        return {"status": "connected", "logged_in": True}
    return {"status": "disconnected", "logged_in": False}

@app.get("/mongodb/login")
async def mongodb_login(request: Request, user_email: str):
    """Initiates the MongoDB OAuth flow (Mocked for Hackathon)."""
    # Use Host header which is reliable on Cloud Run to avoid localhost issues
    host = request.headers.get("host", "localhost:8080")
    scheme = request.headers.get("x-forwarded-proto", "https")
    auth_url = f"{scheme}://{host}/auth?state={user_email}"
    return {"auth_url": auth_url}

@app.get("/auth")
async def auth_page(state: str):
    """Serves a branded mock MongoDB authorization page."""
    html_content = f"""
    <html>
        <head>
            <title>MongoDB Login Simulation</title>
            <style>
                body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; text-align: center; padding-top: 100px; background-color: #f9f9f9; color: #001e2b; }}
                .card {{ background: white; padding: 40px; border-radius: 12px; display: inline-block; box-shadow: 0 4px 12px rgba(0,0,0,0.1); max-width: 400px; }}
                .btn {{ background: #00ed64; color: #001e2b; padding: 15px 30px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block; margin-top: 20px; transition: background 0.2s; }}
                .btn:hover {{ background: #00c654; }}
                .logo {{ background: #001e2b; padding: 15px; border-radius: 8px; margin-bottom: 20px; }}
            </style>
        </head>
        <body>
            <div class="card">
                <img src="https://webassets.mongodb.com/_com_assets/cms/mongodb_logo_white_v2-9602e60.png" width="180" class="logo">
                <h2>Sign in to MongoDB</h2>
                <p>Connect your Atlas account for:<br><b>{state}</b></p>
                <a href="/callback?state={state}&code=hackathon_success" class="btn">Authorize Connection</a>
                <p style="font-size: 12px; color: #888; margin-top: 30px;">(Hackathon Simulation Mode)</p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)

@app.get("/mongodb/register")
async def mongodb_register(user_email: str):
    """Redirects the user to MongoDB Atlas to create an account/cluster."""
    # Official MongoDB Atlas Registration URL
    register_url = "https://www.mongodb.com/cloud/atlas/register"
    return {"register_url": register_url}

@app.get("/callback")
async def oauth_callback(state: str, code: str = "placeholder_code"):
    """Callback for the OAuth flow."""
    user_email = state
    # Mark user as connected
    user_db[user_email] = {
        "connected": True,
        "tokens": {"access_token": "fake_token_" + code}
    }
    return HTMLResponse(content=f"""
    <html>
        <body style="font-family: sans-serif; text-align: center; padding-top: 100px;">
            <h1 style="color: #00ed64;">✓ Successfully Authenticated</h1>
            <p>You can now close this tab and return to your Gmail Sidebar.</p>
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
