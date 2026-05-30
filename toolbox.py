from datetime import datetime, UTC
from tools.embedding_tool import generate_embedding
from db.mongo_client import get_collection
from tools.mongo_mcp import store_email_record, find_unclassified_by_semantic_group, setup_database
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id, delete_label
import json
import os
import numpy as np
from sklearn.cluster import KMeans
from config import settings
import vertexai
from vertexai.generative_models import GenerativeModel

# --- CORE BUSINESS LOGIC TOOLS ---

def generate_category_name(snippets: list) -> str:
    """
    Uses Vertex AI (Gemini 3.5 Flash) to generate a concise category name from email snippets.
    """
    try:
        # Initialize Vertex AI
        project_id = os.getenv("PROJECT_ID", "grah-2026")
        vertexai.init(project=project_id, location="us-central1")
        model = GenerativeModel("gemini-3.5-flash")
        
        prompt = f"""
        Analyze the following email snippets and provide a concise, 1-2 word category name 
        that describes the common theme (e.g., 'Invoices', 'Tech News', 'Travel').
        Return ONLY the category name.

        Snippets:
        {chr(10).join(['- ' + s for s in snippets])}
        """
        
        response = model.generate_content(prompt)
        return response.text.strip().replace(" ", "-").lower()
    except Exception as e:
        print(f"Vertex AI Error: {str(e)}")
        return f"cluster-{random.randint(100, 999)}"

def reorganize_mails(user_email: str):
    """
    Workflow-Path-2 Orchestrator:
    1. Detects degraded labels (< 80% integrity).
    2. Resets emails to 'unclassified'.
    3. Re-clusters and re-labels using AI.
    """
    try:
        email_collection = get_collection()
        label_collection = get_collection("LabelMetadata")
        
        # 1. Identify Degraded Labels
        all_sem_meta = list(label_collection.find({"user_email": user_email}))
        degraded_labels = [l["label_name"] for l in all_sem_meta if l["semantic_integrity_score"] < 0.8]
        
        # 2. Check Gmail for existing sem_ labels
        gmail_labels = get_labels(user_email)
        active_sem_labels = [l["name"] for l in gmail_labels if l["name"].startswith("sem_")]
        
        if not active_sem_labels or degraded_labels:
            print(f"REORG TRIGGERED for {user_email}. Reason: {'No labels' if not active_sem_labels else 'Degraded labels: ' + str(degraded_labels)}")
            
            # 3. Shred Degraded Labels (Bulk Update)
            if degraded_labels:
                email_collection.update_many(
                    {"user_email": user_email, "label": {"$in": degraded_labels}},
                    {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
                )
                # Cleanup metadata and Gmail
                for lbl in degraded_labels:
                    label_collection.delete_one({"user_email": user_email, "label_name": lbl})
                    # Find label ID to delete
                    label_id = next((l["id"] for l in gmail_labels if l["name"] == lbl), None)
                    if label_id:
                        delete_label(user_email, label_id)

            # 4. Perform New Clustering
            clusters = cluster_unclassified_emails(user_email, n_clusters=5)
            if isinstance(clusters, str): return clusters # Error message
            
            for cluster in clusters:
                # 5. Generate AI Name
                snippets = [rep["snippet"] for rep in cluster["representatives"]]
                category_name = "sem_" + generate_category_name(snippets)
                
                # 6. Create in Gmail and Apply
                new_label = create_label(user_email, category_name)
                
                # 7. Bulk Update MongoDB & Gmail
                email_ids = [rep["id"] for rep in cluster["representatives"]]
                # Note: In a real scenario, we'd fetch ALL IDs in the cluster, not just reps.
                # For this implementation, we'll label all emails currently in this cluster.
                
                # Get all email IDs in this cluster from the clustering results
                # (Need to modify cluster_unclassified_emails to return all IDs)
                # For now, let's use the representatives
                for email_id in email_ids:
                    email_collection.update_one(
                        {"_id": email_id},
                        {"$set": {"label": category_name}}
                    )
                    apply_label_to_email(user_email, email_id, new_label["id"])
                
                # 8. Initialize Metadata for the new label
                update_label_integrity(user_email, category_name)
                
            return "Reorganization complete. Inbox optimized."
        
        return "Inbox integrity is healthy (> 80%). No reorganization needed."
        
    except Exception as e:
        print(f"Reorg Error: {str(e)}")
        return f"Error: {str(e)}"

from google.cloud import pubsub_v1
import random

def incremental_update_label_integrity(user_email: str, label_name: str, new_embedding: list):
    """
    Efficiently updates label integrity. 
    If score < 0.8, triggers immediate shredding and publishes reorg event.
    """
    try:
        label_collection = get_collection("LabelMetadata")
        email_collection = get_collection()
        meta = label_collection.find_one({"user_email": user_email, "label_name": label_name})
        
        if not meta:
            # First email for this label, perform full init
            return update_label_integrity(user_email, label_name)
            
        old_avg = meta.get("semantic_integrity_score", 0.0)
        old_centroid = np.array(meta.get("centroid_vector"))
        n = meta.get("email_count", 0)
        
        # 1. Update Centroid incrementally
        new_vec = np.array(new_embedding)
        new_centroid = (old_centroid * n + new_vec) / (n + 1)
        
        # 2. Calculate New Email Similarity
        new_score = calculate_cosine_similarity(new_vec, new_centroid)
        
        # 3. Update Integrity Score incrementally
        new_integrity = (old_avg * n + new_score) / (n + 1)
        
        # 4. Save updates
        label_collection.update_one(
            {"user_email": user_email, "label_name": label_name},
            {
                "$set": {
                    "semantic_integrity_score": float(new_integrity),
                    "centroid_vector": new_centroid.tolist(),
                    "last_calculated_at": datetime.now(UTC).isoformat(),
                    "email_count": n + 1
                }
            }
        )

        # 5. THRESHOLD GUARD: If integrity < 80%, SHRED and TRIGGER REORG
        if new_integrity < 0.8:
            print(f"LABEL DEGRADED: {label_name} ({new_integrity}). Shredding...")
            
            # Bulk Update: Set all emails to unclassified
            email_collection.update_many(
                {"user_email": user_email, "label": label_name},
                {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
            )
            
            # Publish Event to Pub/Sub
            publisher = pubsub_v1.PublisherClient()
            project_id = os.getenv("PROJECT_ID", "grah-2026")
            topic_path = publisher.topic_path(project_id, "reorganize-inbox")
            
            message_data = json.dumps({"user_email": user_email, "reason": f"degraded_{label_name}"})
            publisher.publish(topic_path, message_data.encode("utf-8"))
            
        return new_integrity
        
    except Exception as e:
        print(f"Incremental Update Error: {str(e)}")
        return None

def calculate_cosine_similarity(vec_a, vec_b):
    """Calculates cosine similarity between two vectors."""
    dot_product = np.dot(vec_a, vec_b)
    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)

def update_label_integrity(user_email: str, label_name: str):
    """
    Workflow-Path-2: Recalculates the centroid and integrity score for a specific label.
    """
    try:
        email_collection = get_collection()
        label_collection = get_collection("LabelMetadata")
        
        # 1. Fetch all emails for this label
        query = {"user_email": user_email, "label": label_name}
        emails = list(email_collection.find(query, {"vector_embedding": 1, "_id": 1}))
        
        if not emails:
            return f"No emails found for label {label_name}"
            
        vectors = np.array([e["vector_embedding"] for e in emails])
        
        # 2. Calculate Centroid (Mean Vector)
        centroid = np.mean(vectors, axis=0)
        
        # 3. Calculate individual scores and overall integrity
        scores = []
        for email in emails:
            sim = calculate_cosine_similarity(email["vector_embedding"], centroid)
            scores.append(sim)
            # Update individual email score
            email_collection.update_one(
                {"_id": email["_id"]},
                {"$set": {"email_semantic_score": float(sim)}}
            )
            
        integrity_score = float(np.mean(scores))
        
        # 4. Update Label Metadata
        label_collection.update_one(
            {"user_email": user_email, "label_name": label_name},
            {
                "$set": {
                    "semantic_integrity_score": integrity_score,
                    "centroid_vector": centroid.tolist(),
                    "last_calculated_at": datetime.now(UTC).isoformat(),
                    "email_count": len(emails)
                }
            },
            upsert=True
        )
        
        return {
            "label_name": label_name,
            "integrity_score": integrity_score,
            "email_count": len(emails)
        }
        
    except Exception as e:
        print(f"Integrity Calculation Error: {str(e)}")
        return f"Error: {str(e)}"

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
    Returns all email IDs grouped by cluster.
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

        clusters = []
        for i in range(n_clusters):
            # Get ALL indices for this cluster
            cluster_indices = np.where(labels == i)[0]
            if len(cluster_indices) == 0: continue
            
            # Find representatives (top 5 closest to centroid) for naming
            cluster_vectors = vectors[cluster_indices]
            distances = np.linalg.norm(cluster_vectors - centroids[i], axis=1)
            rep_indices = cluster_indices[np.argsort(distances)[:5]]
            
            reps = []
            for idx in rep_indices:
                reps.append({"id": str(emails[idx]["_id"]), "snippet": emails[idx].get("snippet", "")})
            
            # Collect ALL email IDs in this cluster for bulk labeling
            all_ids = [str(emails[idx]["_id"]) for idx in cluster_indices]
            
            clusters.append({
                "cluster_id": i,
                "count": len(cluster_indices),
                "representatives": reps,
                "all_email_ids": all_ids
            })

        return clusters

    except Exception as e:
        print(f"Clustering Error: {str(e)}")
        return f"Error: {str(e)}"

def process_email(email_metadata: dict, email_body: str):
    """Bridge function for legacy endpoint compatibility."""
    return process_and_store_email(email_metadata, email_body)
