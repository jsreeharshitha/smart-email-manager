from datetime import datetime, UTC
from tools.embedding_tool import generate_embedding
from tools.mongo_mcp import store_email_record, find_unclassified_by_semantic_group
import json

def process_email(email_metadata: dict, email_body: str):
    """
    Coordinates embedding generation and storage of an email.
    Uses MCP tools for storage.
    """
    # 1. Generate embedding
    embedding = generate_embedding(email_body)

    # 2. Prepare document
    document = {
        **email_metadata,
        "vector_embedding": embedding,
        "label": "unclassified",
        "email_semantic_score": 0.0,
        "processed_at": datetime.now(UTC)
    }

    # 3. Store in database via MCP tool
    return store_email_record(document)

def search_and_group_emails(target_group: str):
    """
    Finds unclassified emails that semantically match a target group.
    Uses MCP tools for search.
    """
    print(f"\nSearching for emails matching the group: '{target_group}'...")

    # 1. Embed the target group phrase
    query_embedding = generate_embedding(target_group, model="voyage-3")

    # 2. Perform vector search with pre-filtering via MCP tool
    results_json = find_unclassified_by_semantic_group(query_embedding)
    results = json.loads(results_json)

    # 3. Display results
    if not results:
        print("No matching unclassified emails found.")
        return

    print(f"Found {len(results)} matches:")
    for doc in results:
        print(f"- [{doc['score']:.4f}] {doc.get('subject', 'No Subject')} (from: {doc.get('sender', 'Unknown')})")

if __name__ == "__main__":
    # Example usage: Processing an email
    sample_metadata = {
        "subject": "Invoicing Question",
        "sender": "billing@corp.com",
        "date": datetime.now(UTC).isoformat()
    }
    sample_body = "Can you please send me the invoice for the last quarter?"

    # Store a sample email
    process_email(sample_metadata, sample_body)

    # Demonstrate the semantic search/grouping
    search_and_group_emails("financial documents")

