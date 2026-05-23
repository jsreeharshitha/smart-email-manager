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

def find_unclassified_by_semantic_group(query_embedding: list, limit: int = 5):
    """
    Uses MongoDB $vectorSearch to find "unclassified" emails semantically similar 
    to the query embedding.
    """
    collection = get_collection()
    
    pipeline = [
        {
            "$vectorSearch": {
                "index": "email_vector_search",
                "path": "vector_embedding",
                "queryVector": query_embedding,
                "numCandidates": limit * 10,
                "limit": limit,
                "filter": {"label": "unclassified"}
            }
        },
        {
            "$project": {
                "_id": 1,
                "subject": 1,
                "sender": 1,
                "label": 1,
                "score": {"$meta": "vectorSearchScore"}
            }
        }
    ]
    
    results = list(collection.aggregate(pipeline))
    return results
