from db.mongo_client import get_collection
from datetime import datetime, UTC

def store_email_record(document: dict):
    """
    Stores a document in the MongoDB EmailData collection.
    
    Args:
        document (dict): The email document with embeddings and metadata.
    """
    collection = get_collection()
    insert_result = collection.insert_one(document)
    print(f"Successfully stored email in MongoDB with ID: {insert_result.inserted_id}")
    return insert_result.inserted_id
