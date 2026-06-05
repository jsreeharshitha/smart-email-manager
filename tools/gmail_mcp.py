import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from db.mongo_client import get_client
from config import settings
from datetime import datetime, UTC

# Initialize FastMCP for the Gmail Suite
mcp = FastMCP("GmailSuite")

# Required Scopes
SCOPES = [
    'https://www.googleapis.com/auth/cloud-platform',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.labels',
    'https://www.googleapis.com/auth/gmail.readonly'
]

def get_gmail_service(user_email: str):
    """
    Authenticates and returns the Gmail API service instance.
    Ensures the credentials object has refresh capabilities to prevent library-level crashes.
    """
    try:
        client = get_client()
        db = client[settings.DB_NAME]
        user_session = db["UserSessions"].find_one({"user_email": user_email})

        if not user_session or "credentials" not in user_session:
            raise Exception("No credentials found in MongoDB.")

        creds_data = user_session["credentials"]
        
        # Initialize Credentials with ALL available fields to allow automatic refresh
        # NOTE: We omit the 'scopes' parameter here to allow the refresh_token to 
        # use whatever scopes were originally authorized without causing a mismatch error.
        creds = Credentials(
            token=creds_data.get('access_token'),
            refresh_token=creds_data.get('refresh_token'),
            token_uri=creds_data.get('token_uri', "https://oauth2.googleapis.com/token"),
            client_id=creds_data.get('client_id'),
            client_secret=creds_data.get('client_secret')
        )

        # Proactive Refresh if expired
        if creds.expired or (creds.refresh_token and not creds.valid):
            print(f"[*] Token expired or invalid for {user_email}. Refreshing...")
            try:
                creds.refresh(Request())
                # Update DB with new token
                db["UserSessions"].update_one(
                    {"user_email": user_email},
                    {"$set": {
                        "credentials.access_token": creds.token,
                        "credentials.expiry": creds.expiry.isoformat() if creds.expiry else None
                    }}
                )
                print("[+] Token refreshed and saved to DB.")
            except Exception as re_err:
                print(f"[!] Proactive Refresh Failed: {str(re_err)}. Continuing with existing token...")
        
        return build('gmail', 'v1', credentials=creds)

    except Exception as e:
        print(f"Gmail Service Init Error: {str(e)}")
        raise e

@mcp.tool()
def create_label(user_email: str, label_name: str) -> dict:
    """
    Creates a new custom label in Gmail. Returns a dictionary with 'id' and 'name'.
    """
    try:
        service = get_gmail_service(user_email)
        
        label_object = {
            'name': label_name,
            'labelListVisibility': 'labelShow',
            'messageListVisibility': 'show'
        }
        
        created_label = service.users().labels().create(
            userId='me', 
            body=label_object
        ).execute()
        
        return {"id": created_label["id"], "name": created_label["name"]}

    except HttpError as error:
        if error.resp.status == 409:
            results = service.users().labels().list(userId='me').execute()
            for l in results.get('labels', []):
                if l['name'].lower() == label_name.lower():
                    return {"id": l["id"], "name": l["name"]}
        print(f"HttpError in create_label: {error}")
        return {"id": None, "name": label_name, "error": str(error)}
    except Exception as e:
        print(f"Unexpected error in create_label: {str(e)}")
        return {"id": None, "name": label_name, "error": str(e)}

@mcp.tool()
def delete_label(user_email: str, label_id: str) -> str:
    """
    Deletes a specific label from Gmail.
    """
    try:
        service = get_gmail_service(user_email)
        service.users().labels().delete(userId='me', id=label_id).execute()
        return f"Successfully deleted label with ID: {label_id}"
    except Exception as e:
        return f"Error deleting label: {str(e)}"

@mcp.tool()
def get_labels(user_email: str) -> list:
    """
    Retrieves all labels in the user's Gmail account.
    Enhanced to fetch message counts for 'sem_' labels to identify empty categories.
    """
    try:
        service = get_gmail_service(user_email)
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])

        detailed_labels = []
        for l in labels:
            label_data = {"id": l["id"], "name": l["name"], "messagesTotal": 0}
            # Only fetch details for our semantic labels to save API quota
            if l["name"].startswith("sem_"):
                try:
                    full_l = service.users().labels().get(userId='me', id=l["id"]).execute()
                    label_data["messagesTotal"] = full_l.get("messagesTotal", 0)
                except:
                    pass
            detailed_labels.append(label_data)

        return detailed_labels

    except Exception as e:
        print(f"Error fetching labels: {str(e)}")
        return []

@mcp.tool()
def apply_label_to_email(user_email: str, message_id: str, label_name_or_id: str) -> str:
    """
    Problem 1 Fix: 'One-Label Lock'.
    Strips all other 'sem_' labels before applying the new one.
    """
    try:
        service = get_gmail_service(user_email)
        
        # 1. Resolve target label ID
        all_labels = get_labels(user_email)
        target_label = next((l for l in all_labels if l['id'] == label_name_or_id or l['name'].lower() == label_name_or_id.lower()), None)
        
        if not target_label:
            return f"Error: Label '{label_name_or_id}' not found."
            
        target_id = target_label['id']
        target_name = target_label['name']

        # 2. Identify and strip other 'sem_' labels currently on the message
        msg = service.users().messages().get(userId='me', id=message_id, format='minimal').execute()
        current_label_ids = msg.get('labelIds', [])
        
        labels_to_remove = []
        for lid in current_label_ids:
            # Find the name for this ID
            l_meta = next((l for l in all_labels if l['id'] == lid), None)
            if l_meta and l_meta['name'].startswith('sem_') and lid != target_id:
                labels_to_remove.append(lid)

        # 3. Perform atomic swap
        body = {'addLabelIds': [target_id]}
        if labels_to_remove:
            body['removeLabelIds'] = labels_to_remove
            print(f"[*] One-Label Lock: Stripping {len(labels_to_remove)} sem_ labels from {message_id}")

        service.users().messages().modify(
            userId='me',
            id=message_id,
            body=body
        ).execute()
        
        return f"Successfully applied '{target_name}' and locked email {message_id}"

    except Exception as e:
        return f"Unexpected error in apply_label_to_email: {str(e)}"

@mcp.tool()
def remove_label_from_email(user_email: str, message_id: str, label_name_or_id: str) -> str:
    """
    Removes a label from a specific email message.
    """
    try:
        service = get_gmail_service(user_email)
        all_labels = get_labels(user_email)
        target_label = next((l for l in all_labels if l['id'] == label_name_or_id or l['name'].lower() == label_name_or_id.lower()), None)
        
        if not target_label:
            return f"Error: Label '{label_name_or_id}' not found."
            
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'removeLabelIds': [target_label['id']]}
        ).execute()
        
        return f"Successfully removed label '{target_label['name']}' from message {message_id}"

    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def get_emails_by_id(user_email: str, email_id: str) -> str:
    """
    Retrieves email messages for a specific user.
    """
    try:
        service = get_gmail_service(user_email)
        query = email_id
        results = service.users().messages().list(userId='me', q=query, maxResults=10).execute()
        messages = results.get('messages', [])

        if not messages:
            return f"No emails found for: {email_id}"

        output = f"Emails found for {email_id}:\n"
        for msg in messages:
            msg_detail = service.users().messages().get(userId='me', id=msg['id']).execute()
            headers = msg_detail.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
            date = next((h['value'] for h in headers if h['name'] == 'Date'), "Unknown Date")
            snippet = msg_detail.get('snippet', '')
            output += f"\nID: {msg['id']}\nDate: {date}\nSubject: {subject}\nSnippet: {snippet}\n"

        return output

    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def send_email(user_email: str, to: str, subject: str, body: str) -> str:
    """
    Sends an email from the user's account. Used for proactive notifications.
    """
    try:
        from email.mime.text import MIMEText
        import base64

        service = get_gmail_service(user_email)
        message = MIMEText(body)
        message['to'] = to
        message['from'] = 'me'
        message['subject'] = subject
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()
        
        return f"Email successfully sent to {to}"
    except Exception as e:
        return f"Error sending email: {str(e)}"
