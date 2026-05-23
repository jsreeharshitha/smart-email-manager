import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP for the Gmail Suite
mcp = FastMCP("GmailSuite")

# Required Scopes for managing labels and modifying messages
SCOPES = [
    'https://www.googleapis.com/auth/gmail.labels',
    'https://www.googleapis.com/auth/gmail.modify'
]

def get_gmail_service():
    """Authenticates and returns the Gmail API service instance."""
    creds = None
    # token.json stores the user's access and refresh tokens
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Requires credentials.json from Google Cloud Console
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

@mcp.tool()
def create_label(label_name: str) -> str:
    """
    Creates a new custom label in Gmail.
    
    Args:
        label_name: The name of the label to create (e.g., 'Project Alpha').
    """
    try:
        service = get_gmail_service()
        
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
        # Specific handling for 409 Conflict (Label already exists)
        if error.resp.status == 409:
            return f"Error: The label '{label_name}' already exists in this Gmail account."
        return f"An error occurred: {error}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def get_labels() -> str:
    """
    Retrieves all labels in the user's Gmail account.
    """
    try:
        service = get_gmail_service()
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])

        if not labels:
            return "No labels found."
        
        output = "Gmail Labels:\n"
        for label in labels:
            output += f"- {label['name']} (ID: {label['id']})\n"
        return output

    except HttpError as error:
        return f"An error occurred: {error}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def apply_label_to_email(message_id: str, label_name: str) -> str:
    """
    Applies a label to a specific email message using the label's name.
    
    Args:
        message_id: The ID of the email message to label.
        label_name: The name of the label to apply.
    """
    try:
        service = get_gmail_service()
        
        # 1. Find the label ID by name
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        
        label_id = None
        for label in labels:
            if label['name'].lower() == label_name.lower():
                label_id = label['id']
                break
        
        if not label_id:
            return f"Error: Label '{label_name}' not found. Please create it first using create_label."
            
        # 2. Apply the label to the message
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'addLabelIds': [label_id]}
        ).execute()
        
        return f"Successfully applied label '{label_name}' to message {message_id}"

    except HttpError as error:
        return f"An error occurred: {error}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def get_emails_by_id(email_id: str = "rahulgputcha.dev@gmail.com") -> str:
    """
    Retrieves all email messages associated with a specific email address.

    Args:
        email_id: The email address to search for (sender or recipient).
    """
    try:
        service = get_gmail_service()

        # Search for messages matching the email_id
        # 'q' parameter uses standard Gmail search syntax
        query = email_id
        results = service.users().messages().list(userId='me', q=query, maxResults=10).execute()
        messages = results.get('messages', [])

        if not messages:
            return f"No emails found for: {email_id}"

        output = f"Emails found for {email_id} (showing top {len(messages)}):\n"

        for msg in messages:
            # Get detailed message content
            msg_detail = service.users().messages().get(userId='me', id=msg['id']).execute()

            # Extract headers for Subject and Date
            headers = msg_detail.get('payload', {}).get('headers', [])
            subject = "No Subject"
            date = "Unknown Date"
            for header in headers:
                if header['name'] == 'Subject':
                    subject = header['value']
                if header['name'] == 'Date':
                    date = header['value']

            snippet = msg_detail.get('snippet', '')
            output += f"\n--- ID: {msg['id']} ---\n"
            output += f"Date: {date}\n"
            output += f"Subject: {subject}\n"
            output += f"Snippet: {snippet}\n"

        return output

    except HttpError as error:
        return f"An error occurred: {error}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

@mcp.tool()
def read_emails():

    """Placeholder for reading emails from Gmail."""
    return "Read emails functionality not yet implemented."

@mcp.tool()
def send_email(to: str, subject: str, body: str):
    """Placeholder for sending emails via Gmail."""
    return f"Send email to {to} not yet implemented."
