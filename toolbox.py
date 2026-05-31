from datetime import datetime, UTC
from tools.embedding_tool import generate_embedding
from db.mongo_client import get_collection
from tools.mongo_mcp import store_email_record, find_unclassified_by_semantic_group, setup_database
from tools.gmail_mcp import create_label, get_labels, apply_label_to_email, get_emails_by_id, delete_label, remove_label_from_email
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

UNCLASSIFIED_LABEL = "sem_unclassified"
MAX_SEMANTIC_LABELS = 5

def get_sem_unclassified_id(user_email: str) -> str:
    """Helper to get or create the sem_unclassified label ID."""
    res = create_label(user_email, UNCLASSIFIED_LABEL)
    return res.get("id")

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
    1. Sync Guard: Handles manual deletions.
    2. Problem 2 Fix: 'Hard Cap of 5'.
    3. Re-clusters and re-labels using Gemini 3.
    """
    try:
        project_id = os.getenv("PROJECT_ID", "grah-2026")
        vertexai.init(project=project_id, location="global")
        
        email_collection = get_collection()
        label_collection = get_collection("LabelMetadata")
        
        # 1. Get Gmail State
        gmail_labels = get_labels(user_email)
        active_gmail_sem_labels = [l["name"] for l in gmail_labels if l["name"].startswith("sem_") and l["name"] != UNCLASSIFIED_LABEL]
        unclassified_id = get_sem_unclassified_id(user_email)
        
        # 2. Sync Guard: Detect labels in DB that are missing in Gmail
        db_labels = email_collection.distinct("label", {"user_email": user_email})
        orphaned_labels = [l for l in db_labels if l.startswith("sem_") and l not in active_gmail_sem_labels and l != UNCLASSIFIED_LABEL]
        
        # 3. Identify Degraded Labels
        all_sem_meta = list(label_collection.find({"user_email": user_email}))
        degraded_labels = [l["label_name"] for l in all_sem_meta if l["semantic_integrity_score"] < 0.8]
        
        # Combine labels that need to be reset
        labels_to_shred = list(set(orphaned_labels + degraded_labels))

        # Check if we should trigger reorg
        if not active_gmail_sem_labels or labels_to_shred or len(active_gmail_sem_labels) > MAX_SEMANTIC_LABELS:
            print(f"[*] REORG TRIGGERED for {user_email}.")
            
            # 4. Shred Labels (Bulk Update)
            # If we have too many labels, we shred them all to start fresh with exactly 5
            all_to_shred = labels_to_shred if len(active_gmail_sem_labels) <= MAX_SEMANTIC_LABELS else active_gmail_sem_labels
            
            if all_to_shred:
                email_collection.update_many(
                    {"user_email": user_email, "label": {"$in": all_to_shred}},
                    {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
                )
                label_collection.delete_many({"user_email": user_email, "label_name": {"$in": all_to_shred}})
                
                # If we are over the cap, delete extra labels from Gmail
                if len(active_gmail_sem_labels) > MAX_SEMANTIC_LABELS:
                    for l_name in active_gmail_sem_labels:
                        l_id = next((l["id"] for l in gmail_labels if l["name"] == l_name), None)
                        if l_id: delete_label(user_email, l_id)

            # 5. Perform New Clustering (Capped at 5)
            email_count = email_collection.count_documents({"user_email": user_email, "label": "unclassified"})
            target_clusters = min(MAX_SEMANTIC_LABELS, max(2, email_count // 5 if email_count < 25 else 5))
            
            print(f"[*] Clustering {email_count} emails into {target_clusters} groups...")
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
                    continue
                
                # 8. Apply to ALL emails (Problem 1 Fix: apply_label_to_email now locks to 1 label)
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
                        if unclassified_id:
                            remove_label_from_email(user_email, gmail_id, unclassified_id)
                    except: continue
                
                # 9. Initialize Metadata
                update_label_integrity(user_email, category_name)
                
            return "Reorganization complete. Inbox balanced to 5 themes."
        
        return "Inbox is healthy and balanced."
        
    except Exception as e:
        print(f"Reorg Error: {str(e)}")
        return f"Error: {str(e)}"

def incremental_update_label_integrity(user_email: str, label_name: str, new_embedding: list):
    """
    Efficiently updates label integrity. 
    If score < 0.8, triggers reorg via Pub/Sub.
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
        
        # 1. Update Centroid & Integrity
        new_vec = np.array(new_embedding)
        new_centroid = (old_centroid * n + new_vec) / (n + 1)
        new_score = calculate_cosine_similarity(new_vec, new_centroid)
        new_integrity = (old_avg * n + new_score) / (n + 1)
        
        # 2. Save updates
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

        # 3. THRESHOLD GUARD: If integrity < 80%, SHRED and TRIGGER REORG
        if new_integrity < 0.8:
            print(f"LABEL DEGRADED: {label_name} ({new_integrity}). Shredding...")
            email_collection.update_many(
                {"user_email": user_email, "label": label_name},
                {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
            )
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

def perform_batch_classification(user_email: str):
    """
    Track 2: AGGRESSIVE Batch Classifier.
    Only classifies emails that ARE NOT already categorized.
    """
    try:
        label_collection = get_collection("LabelMetadata")
        email_collection = get_collection()
        
        # 1. Fetch only emails that are 'unclassified' (Stability Rule)
        unclassified = list(email_collection.find({"user_email": user_email, "label": "unclassified"}))
        if not unclassified:
            return "No unclassified emails to process."

        # 2. Fetch all label centroids
        labels_meta = list(label_collection.find({"user_email": user_email}))
        if not labels_meta:
            return "No existing labels to match against."

        gmail_labels = get_labels(user_email)
        label_map = {l["name"]: l["id"] for l in gmail_labels if l["name"].startswith("sem_")}
        unclassified_id = get_sem_unclassified_id(user_email)

        match_count = 0
        for email in unclassified:
            new_vec = np.array(email["vector_embedding"])
            best_match_label = None
            highest_score = -1.0

            for lbl in labels_meta:
                centroid = np.array(lbl["centroid_vector"])
                score = calculate_cosine_similarity(new_vec, centroid)
                if score > highest_score:
                    highest_score = score
                    best_match_label = lbl["label_name"]

            # Always apply the best match (Aggressive but Stable)
            if best_match_label in label_map:
                label_id = label_map[best_match_label]
                gmail_id = email.get("message_id")
                
                email_collection.update_one(
                    {"_id": email["_id"]},
                    {"$set": {"label": best_match_label, "email_semantic_score": float(highest_score)}}
                )
                try:
                    apply_label_to_email(user_email, gmail_id, label_id)
                    if unclassified_id:
                        remove_label_from_email(user_email, gmail_id, unclassified_id)
                    incremental_update_label_integrity(user_email, best_match_label, email["vector_embedding"])
                    match_count += 1
                except: continue
        
        return f"Batch classification complete. Categorized {match_count} emails."

    except Exception as e:
        print(f"Aggressive Batch Classification Error: {str(e)}")
        return f"Error: {str(e)}"

def process_and_store_email(email_metadata: dict, email_body: str):
    """
    Coordinates embedding generation and storage.
    """
    embedding = generate_embedding(email_body)

    document = {
        **email_metadata,
        "vector_embedding": embedding,
        "label": "unclassified",
        "email_semantic_score": 0.0,
        "processed_at": datetime.now(UTC).isoformat(),
        "snippet": email_body[:200]
    }

    res = store_email_record(document)
    
    try:
        user_email = email_metadata.get("user_email")
        gmail_id = email_metadata.get("message_id")
        unclassified_id = get_sem_unclassified_id(user_email)
        if unclassified_id:
            apply_label_to_email(user_email, gmail_id, unclassified_id)
    except: pass

    return res

def cluster_unclassified_emails(user_email: str, n_clusters: int = 5):
    """
    Workflow-2: Performs K-Means clustering on unclassified emails in MongoDB.
    Returns all email IDs grouped by cluster.
    """
    try:
        collection = get_collection()
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
            cluster_indices = np.where(labels == i)[0]
            if len(cluster_indices) == 0: continue
            
            cluster_vectors = vectors[cluster_indices]
            distances = np.linalg.norm(cluster_vectors - centroids[i], axis=1)
            rep_indices = cluster_indices[np.argsort(distances)[:5]]
            
            reps = []
            for idx in rep_indices:
                reps.append({"id": str(emails[idx]["_id"]), "snippet": emails[idx].get("snippet", "")})
            
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
