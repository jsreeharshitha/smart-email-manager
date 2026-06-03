import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY")
DB_NAME = "smart_email_manager"
COLLECTION_NAME = "EmailData"
PREFERENCES_COLLECTION = "UserPreferences"
