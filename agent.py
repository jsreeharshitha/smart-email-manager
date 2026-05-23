from datetime import datetime, UTC
from tools.embedding_tool import generate_embedding
from tools.database_ops import store_email_record

def process_email(email_metadata: dict, email_body: str):
    """
    Coordinates embedding generation and storage of an email.
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
    
    # 3. Store in database
    return store_email_record(document)

if __name__ == "__main__":
    # Example usage
    sample_metadata = {
        "subject": "Hackathon Project Update : 1 2 3",
        "sender": "friend@example.com",
        "date": datetime.now(UTC).isoformat()
    }
    sample_body = "The new email delivery system is ready. Now we need to process the text and store the vector embeddings."
    
    process_email(sample_metadata, sample_body)
