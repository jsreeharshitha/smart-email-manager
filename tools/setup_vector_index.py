from db.mongo_client import get_collection
import time

def create_vector_index():
    collection = get_collection()
    
    # Check if index already exists
    existing_indexes = collection.list_search_indexes()
    for index in existing_indexes:
        if index.get("name") == "email_vector_search":
            print("Index 'email_vector_search' already exists.")
            return

    print("Creating 'email_vector_search' index...")
    
    definition = {
        "fields": [
            {
                "numDimensions": 1024,
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
    
    try:
        result = collection.create_search_index(
            model={
                "name": "email_vector_search",
                "type": "vectorSearch",
                "definition": definition
            }
        )
        print(f"Index creation initiated: {result}")
        
        # Wait for index to be ready (optional, but helpful)
        print("Waiting for index to become queryable...")
        while True:
            indices = list(collection.list_search_indexes(name="email_vector_search"))
            if indices and indices[0].get("queryable"):
                print("Index is now queryable!")
                break
            time.sleep(5)
    except Exception as e:
        print(f"Error creating index: {e}")
        print("Note: Programmatic index creation requires MongoDB Atlas 7.0+.")

if __name__ == "__main__":
    create_vector_index()
