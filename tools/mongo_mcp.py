from mcp.server.fastmcp import FastMCP
from db.mongo_client import get_collection, get_client
from config import settings
from datetime import datetime, UTC
import json
import time

# Initialize FastMCP for MongoDB Operations
mcp = FastMCP("MongoManager")

@mcp.tool()
def setup_database() -> str:
    """
    Ensures the database and collection exist, and configures the Vector Search Index.
    This is an idempotent operation.
    """
    try:
        collection = get_collection()
        
        # 1. Create a dummy document to ensure collection exists
        # MongoDB creates DB/Collections on first write
        collection.update_one(
            {"_id": "metadata_setup"},
            {"$set": {"last_setup": datetime.now(UTC), "status": "active"}},
            upsert=True
        )

        # 2. Setup Vector Search Index
        existing_indexes = list(collection.list_search_indexes())
        index_exists = any(idx.get("name") == "email_vector_search" for idx in existing_indexes)

        if not index_exists:
            definition = {
                "fields": [
                    {
                        "numDimensions": 1024, # Voyage-3 dimension
                        "path": "vector_embedding",
                        "similarity": "cosine",
                        "type": "vector"
                    },
                    {
                        "path": "label",
                        "type": "filter"
                    }
                ]
            }
            collection.create_search_index(
                model={
                    "name": "email_vector_search",
                    "type": "vectorSearch",
                    "definition": definition
                }
            )
            return "Database setup initiated. Collection 'EmailData' verified and 'email_vector_search' index creation started."
        
        return "Database already configured. Collection 'EmailData' and index 'email_vector_search' are ready."
    except Exception as e:
        return f"Error during database setup: {str(e)}"

@mcp.tool()
def store_email_record(document: dict) -> str:
    """
    Stores an email document in the MongoDB EmailData collection.
    
    Args:
        document (dict): The email document with embeddings, metadata, and body.
    """
    try:
        collection = get_collection()
        # Ensure UTC timestamp if not present
        if "processed_at" not in document:
            document["processed_at"] = datetime.now(UTC)
            
        insert_result = collection.insert_one(document)
        return f"Successfully stored email in MongoDB with ID: {insert_result.inserted_id}"
    except Exception as e:
        return f"Error storing email: {str(e)}"

@mcp.tool()
def find_unclassified_by_semantic_group(query_embedding: list, limit: int = 5) -> str:
    """
    Uses MongoDB $vectorSearch to find "unclassified" emails semantically similar 
    to the query embedding.
    
    Args:
        query_embedding (list): The vector embedding of the search query.
        limit (int): Maximum number of results to return.
    """
    try:
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
                    "_id": 0, # Exclude ObjectId for JSON serialization
                    "subject": 1,
                    "sender": 1,
                    "label": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]
        
        results = list(collection.aggregate(pipeline))
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error performing semantic search: {str(e)}"

@mcp.tool()
def get_last_sync_timestamp(user_email: str) -> str:
    """
    Retrieves the timestamp of the last synced email for a specific user.
    Returns ISO string or 'None'.
    """
    try:
        collection = get_collection()
        # Find the latest email for this user based on 'date' field
        # Note: If date is stored as string, sorting might be off if not ISO.
        # But we store as ISO in agent.py
        last_email = collection.find_one(
            {"user_email": user_email},
            sort=[("date", -1)]
        )
        
        if last_email and last_email.get("date"):
            date_val = last_email.get("date")
            if isinstance(date_val, datetime):
                return date_val.isoformat().replace("+00:00", "Z")
            return str(date_val)
        return "None"
    except Exception as e:
        return f"Error fetching last sync: {str(e)}"
