import json
import os
import requests
from google_auth_oauthlib.flow import InstalledAppFlow

# Fix for InsecureTransportError when using http://localhost
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

def run_sync():
    # 1. Setup Environment Targets
    # These scopes match the requirements for Gmail and general GCP management
    SCOPES = [
        'https://www.googleapis.com/auth/cloud-platform',
        'https://www.googleapis.com/auth/gmail.modify',
        'https://www.googleapis.com/auth/gmail.labels',
        'https://www.googleapis.com/auth/gmail.readonly'
    ]
    
    # Discovery logic to find existing setup
    try:
        service_url = os.popen("gcloud run services describe smart-email-manager-agent --platform managed --region us-central1 --format='value(status.url)'").read().strip()
        user_email = os.popen("gcloud config get-value account").read().strip()
        
        if not service_url or not user_email:
            print("[!] Error: Could not detect Service URL or User Email. Ensure you are logged into gcloud and the backend is deployed.")
            return

        if not os.path.exists('client_secret.json'):
            print("[!] Error: 'client_secret.json' not found. Please download it from GCP Credentials console and upload it here.")
            return

        # 2. Run Headless Oauth Loopback Flow
        print(f"[*] Initializing sync for: {user_email}")
        print(f"[*] Targeting Backend: {service_url}")
        
        flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
        flow.redirect_uri = 'http://localhost'

        auth_url, _ = flow.authorization_url(access_type='offline', prompt='consent')
        
        print("\n" + "="*60)
        print("ACTION REQUIRED: AUTHORIZE AGENT")
        print("="*60)
        print(f"\n1. Visit this URL in your browser:\n\n{auth_url}\n")
        print("2. Log in and click 'Advanced' > 'Go to [Project] (unsafe)'")
        print("3. Your browser will eventually fail to load a 'localhost' page.")
        print("4. COPY the FULL URL from your browser's address bar (http://localhost/...)")
        print("="*60 + "\n")

        res_url = input("Paste the FULL localhost URL here: ").strip()

        if not res_url.startswith("http"):
            print("\n[!] Error: Invalid URL. You must paste the complete string starting with http://localhost...")
            return

        # Exchange verification callback parameters for tokens
        print("\n[*] Exchanging code for permanent refresh token...")
        flow.fetch_token(authorization_response=res_url)
        creds = flow.credentials

        # 3. Formulate Refreshable Payload & Synchronize with Cloud Run Agent
        payload = {
            "user_email": user_email,
            "credentials": {
                "access_token": creds.token,
                "refresh_token": creds.refresh_token,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "token_uri": "https://oauth2.googleapis.com/token",
                "expiry": creds.expiry.isoformat() if creds.expiry else None
            }
        }

        print(f"[*] Synchronizing with agent backend...")
        resp = requests.post(f"{service_url}/api/sync-credentials", json=payload)

        if resp.status_code == 200:
            print("\n[+] SUCCESS: Permanent Autonomy Enabled!")
            print("[+] Your agent will now remain authenticated forever.\n")
        else:
            print(f"\n[!] Sync Failed ({resp.status_code}): {resp.text}\n")

    except Exception as e:
        print(f"\n[!] Critical Error: {str(e)}")

if __name__ == "__main__":
    run_sync()
