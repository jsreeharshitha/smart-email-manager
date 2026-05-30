from pymongo import MongoClient
from pymongo.server_api import ServerApi
from config import settings

def get_client():
    """Initializes and returns the MongoDB client."""
    return MongoClient(settings.MONGO_URI, server_api=ServerApi('1'))

def get_collection(collection_name=None):
    """Initializes and returns a MongoDB collection."""
    client = get_client()
    db = client[settings.DB_NAME]
    name = collection_name or settings.COLLECTION_NAME
    return db[name]
