import os
import voyageai
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from datetime import datetime, UTC
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)

def get_mongo_collection():
    """Initializes and returns the MongoDB EmailData collection."""
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    # Use ServerApi('1') as recommended by MongoDB Atlas
    client = MongoClient(mongo_uri, server_api=ServerApi('1'))
    
    # Using 'smart_email_manager' as the default database name
    db = client["smart_email_manager"]
    # Collection name specified in the TODO
    collection = db["EmailData"]
    return collection

def process_and_store_email(email_metadata: dict, email_body: str):
    """
    Takes email metadata and body, generates a vector embedding using Voyage AI,
    and stores the combined data in MongoDB.
    
    Args:
        email_metadata (dict): Metadata of the email (e.g., sender, subject, date).
        email_body (str): The main content of the email to be embedded.
    """
    # Initialize Voyage AI client (it automatically picks up VOYAGE_API_KEY from env)
    vo = voyageai.Client(api_key=os.getenv("VOYAGE_API_KEY"))
    
    # 1. Get embedding from Voyage AI
    # We use voyage-3 as a default model for general text embeddings
    print("Generating vector embedding via Voyage AI...")
    result = vo.embed([email_body], model="voyage-3", input_type="document")
    embedding = result.embeddings[0]
    
    # 2. Prepare the document based on the required schema
    # Flatten metadata fields to the top level
    document = {
        **email_metadata,
        "vector_embedding": embedding,
        "label": "unclassified",           # default: unclassified
        "email_semantic_score": 0.0,       # default: 0%
        "processed_at": datetime.now(UTC)
    }
    
    # 3. Store the document in MongoDB Document EmailData
    collection = get_mongo_collection()
    insert_result = collection.insert_one(document)
    print(f"Successfully stored email in MongoDB with ID: {insert_result.inserted_id}")
    
    return insert_result.inserted_id

if __name__ == "__main__":
    # Example usage for testing purposes
    sample_metadata = {
        "subject": "Hackathon Project Update",
        "sender": "friend@example.com",
        "date": datetime.now(UTC).isoformat()
    }
    sample_body = "The new email delivery system is ready. Now we need to process the text and store the vector embeddings."
    
    # Ensure you have your .env file setup before running this directly
    if os.getenv("VOYAGE_API_KEY") and os.getenv("MONGO_URI"):
        process_and_store_email(sample_metadata, sample_body)
    else:
        print("Please set VOYAGE_API_KEY and MONGO_URI in your .env file to run the example.")
