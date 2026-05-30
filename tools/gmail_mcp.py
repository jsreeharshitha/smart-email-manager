import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from db.mongo_client import get_client
from config import settings

# Initialize FastMCP for the Gmail Suite
mcp = FastMCP("GmailSuite")

# Required Scopes
SCOPES = [
    'https://www.googleapis.com/auth/gmail.labels',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.readonly'
]

def get_gmail_service(user_email: str):
    """
    Authenticates and returns the Gmail API service instance.
    Supports both short-lived tokens and persistent refresh tokens.
    """
    try:
        client = get_client()
        db = client[settings.DB_NAME]
        user_session = db["UserSessions"].find_one({"user_email": user_email})

        if not user_session or "credentials" not in user_session:
            raise Exception(f"Auth missing for {user_email}. Run the Sync script in Cloud Shell.")

        creds_data = user_session["credentials"]
        
        # Build full credentials if refresh token is available
        creds = Credentials(
            token=creds_data.get('access_token'),
            refresh_token=creds_data.get('refresh_token'),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=creds_data.get('client_id'),
            client_secret=creds_data.get('client_secret'),
            scopes=SCOPES
        )

        # Autonomously refresh if expired and we have the means
        if creds.expired and creds.refresh_token:
            print(f"Token expired for {user_email}. Attempting autonomous refresh...")
            creds.refresh(Request())
            # Save the new access token back to MongoDB
            db["UserSessions"].update_one(
                {"user_email": user_email},
                {"$set": {
                    "credentials.access_token": creds.token,
                    "updated_at": datetime.now(UTC).isoformat()
                }}
            )
            print("Autonomous refresh successful.")

        return build('gmail', 'v1', credentials=creds)

    except Exception as e:
        print(f"Gmail Auth Error for {user_email}: {str(e)}")
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
            # Handle existing label: fetch and return its ID
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
    Retrieves all labels in the user's Gmail account as a list of dictionaries.
    Each dictionary contains 'id' and 'name'.
    """
    try:
        service = get_gmail_service(user_email)
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])

        return [{"id": l["id"], "name": l["name"]} for l in labels]

    except Exception as e:
        print(f"Error fetching labels: {str(e)}")
        return []

@mcp.tool()
def apply_label_to_email(user_email: str, message_id: str, label_name_or_id: str) -> str:
    """
    Applies a label to a specific email message.
    """
    try:
        service = get_gmail_service(user_email)
        
        # Determine if label_name_or_id is an ID or a name
        # We'll fetch all labels to be safe
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        
        label_id = None
        # Check if it matches an ID first
        if any(l['id'] == label_name_or_id for l in labels):
            label_id = label_name_or_id
        else:
            # Try to match by name
            for label in labels:
                if label['name'].lower() == label_name_or_id.lower():
                    label_id = label['id']
                    break
        
        if not label_id:
            return f"Error: Label '{label_name_or_id}' not found."
            
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'addLabelIds': [label_id]}
        ).execute()
        
        return f"Successfully applied label '{label_name}' to message {message_id}"

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
