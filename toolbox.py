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
from google.cloud import pubsub_v1
import random
from bson import ObjectId
import re

# --- CORE BUSINESS LOGIC TOOLS ---

def generate_category_name(snippets: list) -> str:
    """
    Uses Vertex AI (Gemini 3.5 Flash) to generate a concise category name from email snippets.
    """
    try:
        project_id = os.getenv("PROJECT_ID", "grah-2026")
        vertexai.init(project=project_id, location="global")
        model = GenerativeModel("gemini-3.5-flash")
        
        prompt = f"""
        Analyze the following email snippets and provide a concise, 1-2 word category name 
        that describes the common theme (e.g., 'Invoices', 'Tech News', 'Travel').
        Return ONLY the category name.

        Snippets:
        {chr(10).join(['- ' + s for s in snippets])}
        """
        
        response = model.generate_content(prompt)
        name = response.text.strip().replace(" ", "-").lower()
        name = re.sub(r'[^a-z0-9\-]', '', name)
        return name if name else "uncategorized"
    except Exception as e:
        print(f"Vertex AI Error: {str(e)}")
        return f"cluster-{random.randint(100, 999)}"

def reorganize_mails(user_email: str):
    """
    Workflow-Path-2 Orchestrator:
    1. Detects manual deletions in Gmail and syncs MongoDB.
    2. Detects degraded labels (< 80% integrity).
    3. Re-clusters and re-labels using Gemini 3.
    """
    try:
        project_id = os.getenv("PROJECT_ID", "grah-2026")
        vertexai.init(project=project_id, location="global")
        
        email_collection = get_collection()
        label_collection = get_collection("LabelMetadata")
        
        # 1. Get Gmail State
        gmail_labels = get_labels(user_email)
        active_gmail_sem_labels = [l["name"] for l in gmail_labels if l["name"].startswith("sem_")]
        
        # 2. Sync Guard: Detect labels in DB that are missing in Gmail (Manual Deletions)
        db_labels = email_collection.distinct("label", {"user_email": user_email})
        orphaned_labels = [l for l in db_labels if l.startswith("sem_") and l not in active_gmail_sem_labels]
        
        # 3. Identify Degraded Labels
        all_sem_meta = list(label_collection.find({"user_email": user_email}))
        degraded_labels = [l["label_name"] for l in all_sem_meta if l["semantic_integrity_score"] < 0.8]
        
        # Combine labels that need to be reset
        labels_to_shred = list(set(orphaned_labels + degraded_labels))

        if not active_gmail_sem_labels or labels_to_shred:
            print(f"REORG TRIGGERED for {user_email}.")
            if orphaned_labels: print(f"-> Reason: Orphaned labels found in DB: {orphaned_labels}")
            if degraded_labels: print(f"-> Reason: Degraded labels: {degraded_labels}")
            if not active_gmail_sem_labels: print(f"-> Reason: No sem_ labels in Gmail.")

            # 4. Shred Labels (Bulk Update)
            if labels_to_shred:
                email_collection.update_many(
                    {"user_email": user_email, "label": {"$in": labels_to_shred}},
                    {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
                )
                label_collection.delete_many({"user_email": user_email, "label_name": {"$in": labels_to_shred}})

            # 5. Perform New Clustering
            email_count = email_collection.count_documents({"user_email": user_email, "label": "unclassified"})
            target_clusters = 5
            if email_count < 5 and email_count >= 2:
                target_clusters = 2
                print(f"Low email count ({email_count}). Adjusting to {target_clusters} clusters.")
            
            clusters = cluster_unclassified_emails(user_email, n_clusters=target_clusters)
            if isinstance(clusters, str): 
                print(f"Reorg Aborted: {clusters}")
                return clusters
            
            for cluster in clusters:
                # 6. Generate AI Name
                snippets = [rep["snippet"] for rep in cluster["representatives"]]
                raw_name = generate_category_name(snippets)
                category_name = "sem_" + raw_name
                
                # 7. Create in Gmail
                new_label = create_label(user_email, category_name)
                label_id = new_label.get("id")
                
                if not label_id:
                    print(f"Failed to create label {category_name}. Skipping cluster.")
                    continue
                
                # 8. Apply to ALL emails
                email_data_list = cluster["all_emails"]
                for item in email_data_list:
                    mongo_id = item["mongo_id"]
                    gmail_id = item["gmail_id"]
                    
                    email_collection.update_one(
                        {"_id": ObjectId(mongo_id)}, 
                        {"$set": {"label": category_name}}
                    )
                    try:
                        apply_label_to_email(user_email, gmail_id, label_id)
                    except: continue
                
                # 9. Initialize Metadata
                update_label_integrity(user_email, category_name)
                
            return "Reorganization complete."
        
        return "Inbox is healthy."
        
    except Exception as e:
        print(f"Reorg Error: {str(e)}")
        return f"Error: {str(e)}"

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
            
            # Bulk Update: Set all emails in this label to unclassified
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
        
        query = {"user_email": user_email, "label": label_name}
        emails = list(email_collection.find(query, {"vector_embedding": 1, "message_id": 1, "_id": 1}))
        
        if not emails:
            return f"No emails found for label {label_name}"
            
        vectors = np.array([e["vector_embedding"] for e in emails])
        centroid = np.mean(vectors, axis=0)
        
        scores = []
        for email in emails:
            sim = calculate_cosine_similarity(email["vector_embedding"], centroid)
            scores.append(sim)
            email_collection.update_one(
                {"_id": email["_id"]},
                {"$set": {"email_semantic_score": float(sim)}}
            )
            
        integrity_score = float(np.mean(scores))
        
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

def auto_classify_mail(user_email: str, gmail_id: str, mongo_id: ObjectId, embedding: list):
    """
    Real-time Classifier: Compares new mail against existing healthy labels.
    If similarity > 80%, classifies immediately.
    """
    try:
        label_collection = get_collection("LabelMetadata")
        email_collection = get_collection()
        
        # 1. Fetch all existing labels for this user
        labels = list(label_collection.find({"user_email": user_email}))
        if not labels:
            return False

        new_vec = np.array(embedding)
        best_match = None
        highest_score = 0.0

        # 2. Find the closest centroid
        for lbl in labels:
            centroid = np.array(lbl["centroid_vector"])
            score = calculate_cosine_similarity(new_vec, centroid)
            if score > highest_score:
                highest_score = score
                best_match = lbl["label_name"]

        # 3. Apply if above 80% threshold
        if highest_score >= 0.8:
            print(f"REAL-TIME MATCH: Email matched '{best_match}' with {highest_score:.2f} similarity.")
            
            # Update MongoDB
            email_collection.update_one(
                {"_id": mongo_id},
                {"$set": {"label": best_match, "email_semantic_score": float(highest_score)}}
            )
            
            # Update Gmail
            gmail_labels = get_labels(user_email)
            label_id = next((l["id"] for l in gmail_labels if l["name"] == best_match), None)
            if label_id:
                apply_label_to_email(user_email, gmail_id, label_id)
                # 4. Trigger incremental update to keep centroid healthy
                incremental_update_label_integrity(user_email, best_match, embedding)
                return True
                
        return False
    except Exception as e:
        print(f"Auto-Classification Error: {str(e)}")
        return False

def process_and_store_email(email_metadata: dict, email_body: str):
    """
    Coordinates embedding generation and storage.
    Now includes real-time classification attempt.
    """
    user_email = email_metadata.get("user_email")
    gmail_id = email_metadata.get("message_id")
    embedding = generate_embedding(email_body)

    # Initial document
    document = {
        **email_metadata,
        "vector_embedding": embedding,
        "label": "unclassified",
        "email_semantic_score": 0.0,
        "processed_at": datetime.now(UTC).isoformat(),
        "snippet": email_body[:200]
    }

    # Save to MongoDB first
    res = store_email_record(document)
    mongo_id = ObjectId(res.get("id")) if isinstance(res, dict) else None

    # Attempt Real-time Classification
    if mongo_id:
        auto_classify_mail(user_email, gmail_id, mongo_id, embedding)

    return res

def cluster_unclassified_emails(user_email: str, n_clusters: int = 5):
    """
    Workflow-2: Performs K-Means clustering on unclassified emails in MongoDB.
    Returns all email IDs grouped by cluster.
    """
    try:
        collection = get_collection()
        # Fetch both Mongo _id and Gmail message_id
        query = {"user_email": user_email, "label": "unclassified"}
        emails = list(collection.find(query, {"_id": 1, "message_id": 1, "vector_embedding": 1, "subject": 1, "snippet": 1}))

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
            
            # Collect ALL IDs in this cluster for bulk labeling
            all_emails = []
            for idx in cluster_indices:
                all_emails.append({
                    "mongo_id": str(emails[idx]["_id"]),
                    "gmail_id": emails[idx].get("message_id")
                })
            
            clusters.append({
                "cluster_id": i,
                "count": len(cluster_indices),
                "representatives": reps,
                "all_emails": all_emails
            })

        return clusters

    except Exception as e:
        print(f"Clustering Error: {str(e)}")
        return f"Error: {str(e)}"

def process_email(email_metadata: dict, email_body: str):
    """Bridge function for legacy endpoint compatibility."""
    return process_and_store_email(email_metadata, email_body)
