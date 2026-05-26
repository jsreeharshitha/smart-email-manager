from google.adk.agents import Agent
from datetime import datetime, UTC
from tools.embedding_tool import generate_embedding
from tools.mongo_mcp import store_email_record, find_unclassified_by_semantic_group, setup_database
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id
import json
import os

# 1. Define high-level tools for the ADK Agent
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
        "processed_at": datetime.now(UTC)
    }

    # Store in database via MongoDB MCP tool
    return store_email_record(document)

# 2. Initialize the ADK Agent
# Note: Using 'gemini-1.5-pro' to represent the next-gen 'gemini-3' logic.
root_agent = Agent(
    name="SmartEmailManagerAgent",
    model="gemini-1.5-pro", 
    instruction="""
    You are the high-performance Smart Email Manager Agent (ADK Edition).
    Your goal is to provide 'zero-touch' semantic email organization for users.
    
    Advanced Capabilities:
    - Environment Self-Healing: Use 'setup_database' to ensure the environment is ready.
    - Deep Semantic Sync: Use 'process_and_store_email' for vector-indexed storage.
    - Autonomous Labeling: Use 'find_unclassified_by_semantic_group' to reason about labels.
    - Gmail Orchestration: Manage 'create_label' and 'apply_label_to_email' workflows.
    
    Logic Chain:
    1. If new emails arrive, sync them to MongoDB using 'process_and_store_email'.
    2. Analyze the context of unclassified emails via semantic search.
    3. If multiple emails share a context (e.g. 'Invoices', 'Travel'), check if a Gmail label exists.
    4. If no label exists, create it.
    5. Apply the label to all relevant messages to organize the user's inbox.
    """,
    tools=[
        setup_database,
        process_and_store_email,
        find_unclassified_by_semantic_group,
        create_label,
        get_labels,
        apply_label_to_email,
        get_emails_by_id
    ]
)

# 3. Legacy/Direct Functions (for compatibility with FastAPI endpoints)

def process_email(email_metadata: dict, email_body: str):
    """Bridge function for FastAPI /mongodb/sync endpoint."""
    return process_and_store_email(email_metadata, email_body)

def search_and_group_emails(target_group: str):
    """
    Performs a semantic search and displays results.
    Can be invoked directly or via the ADK Agent.
    """
    print(f"\nSearching for emails matching the group: '{target_group}'...")

    # Embed the target group phrase
    query_embedding = generate_embedding(target_group, model="voyage-3")

    # Perform vector search via MCP tool
    results_json = find_unclassified_by_semantic_group(query_embedding)
    results = json.loads(results_json)

    if not results:
        print("No matching unclassified emails found.")
        return

    print(f"Found {len(results)} matches:")
    for doc in results:
        print(f"- [{doc['score']:.4f}] {doc.get('subject', 'No Subject')} (from: {doc.get('sender', 'Unknown')})")

if __name__ == "__main__":
    # Example: How to run the agent in a 'chat' or 'instruction' mode
    # In a real deployment, this would be triggered by an event or API call.
    print("Smart Email Manager Agent (ADK) Initialized.")
    
    # You could run: root_agent.run("Organize my billing emails into a new 'Finance' label")
