from pymongo import MongoClient
from pymongo.server_api import ServerApi
from config import settings

_mongo_client = None

def get_client():
    """Initializes and returns the MongoDB client, caching it for reuse."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(
            settings.MONGO_URI, 
            server_api=ServerApi('1'),
            connectTimeoutMS=10000, 
            socketTimeoutMS=10000,
            serverSelectionTimeoutMS=10000,
            retryWrites=True,
            retryReads=True,
            maxPoolSize=10
        )
    return _mongo_client

def get_collection(collection_name=None):
    """Initializes and returns a MongoDB collection."""
    client = get_client()
    db = client[settings.DB_NAME]
    name = collection_name or settings.COLLECTION_NAME
    return db[name]
