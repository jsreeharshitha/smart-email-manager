from datetime import datetime, UTC
from tools.embedding_tool import generate_embedding
from tools.mongo_mcp import store_email_record, find_unclassified_by_semantic_group, setup_database, get_collection
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id
import json
import os
import numpy as np
from sklearn.cluster import KMeans
from config import settings

# --- CORE BUSINESS LOGIC TOOLS ---
# These functions are registered as MCP tools in main.py

def process_and_store_email(email_metadata: dict, email_body: str):
    """
    Coordinates embedding generation and storage of an email in MongoDB.
    """
    embedding = generate_embedding(email_body)

    document = {
        **email_metadata,
        "vector_embedding": embedding,
        "label": "unclassified",
        "email_semantic_score": 0.0,
        "processed_at": datetime.now(UTC).isoformat(),
        "snippet": email_body[:200] # Store snippet for clustering previews
    }

    return store_email_record(document)

def cluster_unclassified_emails(user_email: str, n_clusters: int = 5):
    """
    Workflow-2: Performs K-Means clustering on unclassified emails in MongoDB.
    Returns the representative emails (closest to centroids) for each cluster.
    """
    try:
        collection = get_collection()
        query = {"user_email": user_email, "label": "unclassified"}
        emails = list(collection.find(query, {"_id": 1, "vector_embedding": 1, "subject": 1, "snippet": 1}))

        if len(emails) < n_clusters:
            return f"Not enough emails to form {n_clusters} clusters. Found {len(emails)}."

        vectors = np.array([e["vector_embedding"] for e in emails])

        kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10).fit(vectors)
        centroids = kmeans.cluster_centers_
        labels = kmeans.labels_

        cluster_representatives = []
        for i in range(n_clusters):
            cluster_indices = np.where(labels == i)[0]
            if len(cluster_indices) == 0: continue
            
            cluster_vectors = vectors[cluster_indices]
            distances = np.linalg.norm(cluster_vectors - centroids[i], axis=1)
            closest_indices = cluster_indices[np.argsort(distances)[:5]]
            
            reps = []
            for idx in closest_indices:
                email = emails[idx]
                reps.append({
                    "id": str(email["_id"]),
                    "subject": email.get("subject", "No Subject"),
                    "snippet": email.get("snippet", "")
                })
            
            cluster_representatives.append({
                "cluster_id": i,
                "count": len(cluster_indices),
                "representatives": reps
            })

        return cluster_representatives

    except Exception as e:
        print(f"Clustering Error: {str(e)}")
        return f"Error during clustering: {str(e)}"

def process_email(email_metadata: dict, email_body: str):
    """Bridge function for legacy endpoint compatibility."""
    return process_and_store_email(email_metadata, email_body)
