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
    Authenticates and returns the Gmail API service instance for a specific user.
    Retrieves credentials from MongoDB UserSessions.
    """
    try:
        client = get_client()
        db = client[settings.DB_NAME]
        user_session = db["UserSessions"].find_one({"user_email": user_email})

        if not user_session or "credentials" not in user_session:
            # Fallback for local testing if credentials.json exists (optional, remove for pure production)
            if os.path.exists('token.json'):
                 creds = Credentials.from_authorized_user_file('token.json', SCOPES)
                 return build('gmail', 'v1', credentials=creds)
            raise Exception(f"No Gmail credentials found for user: {user_email}.")

        creds_data = user_session["credentials"]
        creds = Credentials(
            token=creds_data.get('access_token'),
            refresh_token=creds_data.get('refresh_token'),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ.get("GMAIL_CLIENT_ID"),
            client_secret=os.environ.get("GMAIL_CLIENT_SECRET"),
            scopes=SCOPES
        )

        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save the updated token back to MongoDB
            db["UserSessions"].update_one(
                {"user_email": user_email},
                {"$set": {"credentials.access_token": creds.token, "updated_at": "auto-refreshed"}}
            )

        return build('gmail', 'v1', credentials=creds)

    except Exception as e:
        print(f"Gmail Auth Error for {user_email}: {str(e)}")
        raise e

@mcp.tool()
def create_label(user_email: str, label_name: str) -> str:
    """
    Creates a new custom label in Gmail.
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
        
        return f"Successfully created label: {created_label['name']} (ID: {created_label['id']})"

    except HttpError as error:
        if error.resp.status == 409:
            return f"Error: The label '{label_name}' already exists."
        return f"An error occurred: {error}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def get_labels(user_email: str) -> str:
    """
    Retrieves all labels in the user's Gmail account.
    """
    try:
        service = get_gmail_service(user_email)
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])

        if not labels:
            return "No labels found."
        
        output = "Gmail Labels:\n"
        for label in labels:
            output += f"- {label['name']} (ID: {label['id']})\n"
        return output

    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def apply_label_to_email(user_email: str, message_id: str, label_name: str) -> str:
    """
    Applies a label to a specific email message.
    """
    try:
        service = get_gmail_service(user_email)
        
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        
        label_id = None
        for label in labels:
            if label['name'].lower() == label_name.lower():
                label_id = label['id']
                break
        
        if not label_id:
            return f"Error: Label '{label_name}' not found."
            
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
