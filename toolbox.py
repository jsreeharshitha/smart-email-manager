from datetime import datetime, UTC
from tools.embedding_tool import generate_embedding
from tools.mongo_mcp import store_email_record, find_unclassified_by_semantic_group, setup_database
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id

# --- CORE BUSINESS LOGIC TOOLS ---
# These functions are registered as MCP tools in main.py

def process_and_store_email(metadata: dict, body: str):
    """
    Coordinates embedding generation and storage of an email in MongoDB.
    
    Args:
        metadata (dict): Email headers (subject, sender, date, etc.)
        body (str): The plain text content of the email.
    """
    # Generate vector embedding using Voyage AI (configured in embedding_tool)
    embedding = generate_embedding(body)

    # Prepare the MongoDB document
    document = {
        **metadata,
        "vector_embedding": embedding,
        "label": "unclassified",
        "email_semantic_score": 0.0,
        "processed_at": datetime.now(UTC).isoformat()
    }

    # Store in database via MongoDB MCP tool
    return store_email_record(document)

def search_and_group_emails(target_group: str):
    """
    Performs a semantic search and returns results for grouping.
    """
    # Embed the target group phrase
    query_embedding = generate_embedding(target_group)

    # Perform vector search via MCP tool
    results_json = find_unclassified_by_semantic_group(query_embedding)
    return results_json

def process_email(email_metadata: dict, email_body: str):
    """Bridge function for legacy endpoint compatibility."""
    return process_and_store_email(email_metadata, email_body)
