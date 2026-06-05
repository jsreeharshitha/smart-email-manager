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
    Ensures the database and collections exist, and configures the Vector Search Indexes.
    This is an idempotent operation.
    """
    try:
        # 1. Setup EmailData Index
        email_collection = get_collection(settings.COLLECTION_NAME)
        email_collection.update_one(
            {"_id": "metadata_setup"},
            {"$set": {"last_setup": datetime.now(UTC), "status": "active"}},
            upsert=True
        )

        existing_email_indexes = list(email_collection.list_search_indexes())
        email_index_exists = any(idx.get("name") == "email_vector_search" for idx in existing_email_indexes)

        if not email_index_exists:
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
            email_collection.create_search_index(
                model={
                    "name": "email_vector_search",
                    "type": "vectorSearch",
                    "definition": definition
                }
            )
            msg = "EmailData index setup initiated. "
        else:
            msg = "EmailData index ready. "

        # 2. Setup UserPreferences Index
        pref_collection = get_collection(settings.PREFERENCES_COLLECTION)
        pref_collection.update_one(
            {"_id": "metadata_setup"},
            {"$set": {"last_setup": datetime.now(UTC), "status": "active"}},
            upsert=True
        )

        existing_pref_indexes = list(pref_collection.list_search_indexes())
        pref_index_exists = any(idx.get("name") == "preference_vector_search" for idx in existing_pref_indexes)

        if not pref_index_exists:
            definition = {
                "fields": [
                    {
                        "numDimensions": 1024,
                        "path": "vector_embedding",
                        "similarity": "cosine",
                        "type": "vector"
                    },
                    {
                        "path": "user_id",
                        "type": "filter"
                    }
                ]
            }
            pref_collection.create_search_index(
                model={
                    "name": "preference_vector_search",
                    "type": "vectorSearch",
                    "definition": definition
                }
            )
            msg += "UserPreferences index setup initiated."
        else:
            msg += "UserPreferences index ready."
            
        return msg
    except Exception as e:
        return f"Error during database setup: {str(e)}"

@mcp.tool()
def search_user_preferences(user_email: str, query_embedding: list, limit: int = 3) -> str:
    """
    Uses MongoDB $vectorSearch to find relevant user preferences (long-term memory).
    """
    try:
        collection = get_collection(settings.PREFERENCES_COLLECTION)
        
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "preference_vector_search",
                    "path": "vector_embedding",
                    "queryVector": query_embedding,
                    "numCandidates": limit * 10,
                    "limit": limit,
                    "filter": {"user_id": user_email}
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "sender_domain": 1,
                    "llm_semantic_note": 1,
                    "structured_rule": 1,
                    "confidence_score": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]
        
        results = list(collection.aggregate(pipeline))
        return json.dumps(results, indent=2)
    except Exception as e:
        return f"Error performing memory search: {str(e)}"

@mcp.tool()
def store_email_record(document: dict) -> dict:
    """
    Stores or updates an email document in the MongoDB EmailData collection.
    Uses 'upsert' based on message_id to prevent duplicates.
    """
    try:
        collection = get_collection()
        msg_id = document.get("message_id")
        
        if not msg_id:
            raise Exception("Document must contain a 'message_id' for deduplication.")

        if "processed_at" not in document:
            document["processed_at"] = datetime.now(UTC).isoformat()
            
        # 1. Perform Upsert (Replace if exists, Insert if new)
        collection.replace_one(
            {"message_id": msg_id},
            document,
            upsert=True
        )
        
        # 2. Fetch and return the (new or existing) ID for downstream processing
        updated_doc = collection.find_one({"message_id": msg_id}, {"_id": 1})
        return {
            "status": "success",
            "id": str(updated_doc["_id"]),
            "message_id": msg_id
        }
    except Exception as e:
        print(f"MongoDB Store Error: {str(e)}")
        return {"status": "error", "message": str(e)}

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

@mcp.tool()
def update_email_lifecycle(message_id: str, action: str) -> str:
    """
    Updates the lifecycle metadata of an email (e.g., 'first_read', 'archived').
    
    Args:
        message_id (str): The Gmail message ID.
        action (str): The action performed ('read', 'archived', 'deleted').
    """
    try:
        collection = get_collection()
        field_name = f"lifecycle_{action}_at"
        
        # We only set the timestamp if it doesn't already exist (to capture the FIRST occurrence)
        result = collection.update_one(
            {"message_id": message_id},
            {"$setOnInsert": {field_name: datetime.now(UTC).isoformat()}},
            upsert=False
        )
        
        # If it was a 'read' action, we also update the last_accessed_at field
        if action == "read":
            collection.update_one(
                {"message_id": message_id},
                {"$set": {"last_accessed_at": datetime.now(UTC).isoformat()}}
            )

        return f"Successfully updated lifecycle for {message_id} with action: {action}"
    except Exception as e:
        return f"Error updating lifecycle: {str(e)}"
