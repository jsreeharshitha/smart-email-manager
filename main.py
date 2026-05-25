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
async def mongodb_login(request: Request, user_email: str, base_url: str = None):
    """Initiates the MongoDB OAuth flow (Mocked for Hackathon)."""
    # Use the base_url provided by the client (Gmail Add-on) or detect it
    if not base_url:
        host = request.headers.get("host", "localhost:8080")
        scheme = request.headers.get("x-forwarded-proto", "https")
        base_url = f"{scheme}://{host}"
    
    base_url = base_url.rstrip('/')
    auth_url = f"{base_url}/auth?state={user_email}"
    return {"auth_url": auth_url}

@app.get("/auth")
async def auth_page(state: str):
    """Serves a professional 'Sign in with Google' simulation for MongoDB."""
    user_email = state
    html_content = f"""
    <html>
        <head>
            <title>Sign in - MongoDB Atlas</title>
            <style>
                body {{ font-family: 'Roboto', arial, sans-serif; background-color: #fff; margin: 0; display: flex; align-items: center; justify-content: center; height: 100vh; }}
                .container {{ border: 1px solid #dadce0; border-radius: 8px; width: 450px; padding: 48px 40px 36px; text-align: center; }}
                .logo {{ width: 120px; margin-bottom: 24px; }}
                h1 {{ font-size: 24px; font-weight: 400; margin-bottom: 8px; }}
                p {{ color: #202124; font-size: 16px; margin-bottom: 32px; }}
                .user-box {{ border: 1px solid #dadce0; border-radius: 20px; display: inline-flex; align-items: center; padding: 5px 15px; margin-bottom: 30px; }}
                .user-icon {{ background: #4285f4; color: white; width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 14px; margin-right: 10px; }}
                .btn {{ background-color: #001e2b; color: white; border: none; border-radius: 4px; padding: 10px 24px; font-size: 14px; font-weight: 500; cursor: pointer; text-decoration: none; display: block; }}
                .btn:hover {{ background-color: #00303e; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }}
                .footer {{ color: #70757a; font-size: 12px; margin-top: 40px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <img src="https://webassets.mongodb.com/_com_assets/cms/mongodb_logo_white_v2-9602e60.png" class="logo" style="background: #001e2b; padding: 10px; border-radius: 4px;">
                <h1>Sign in</h1>
                <p>to continue to Rapid Agent Gmail Suite</p>
                <div class="user-box">
                    <div class="user-icon">{user_email[0].upper()}</div>
                    <span>{user_email}</span>
                </div>
                <a href="/callback?state={user_email}&code=google_oauth_success" class="btn">Continue as {user_email.split('@')[0]}</a>
                <div class="footer">
                    MongoDB Atlas uses your Google identity to securely connect<br>to your email management clusters.
                </div>
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
