from datetime import datetime, UTC, timedelta
from tools.embedding_tool import generate_embedding
from db.mongo_client import get_collection, get_client
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

DEFAULT_CONFIG = {
    "MAX_SEMANTIC_LABELS": 5,
    "REORG_COOLDOWN_HOURS": 1,
    "BACKLOG_THRESHOLD": 15,
    "DEFAULT_SIMILARITY_THRESHOLD": 0.85,
    "ADAPTIVE_THRESHOLD_HYSTERESIS": 0.85,
    "BATCH_CLASSIFICATION_FREQUENCY": 10,
    "AUTO_SYNC_NEW_EMAILS": True
}

def get_user_settings(user_email: str) -> dict:
    """
    Fetches user-specific agent configurations from MongoDB.
    Ensures the agent behavior can be tuned via the UI without code changes.
    """
    try:
        client = get_client()
        db = client[settings.DB_NAME]
        user_session = db["UserSessions"].find_one({"user_email": user_email})
        
        user_settings = user_session.get("agent_settings", {}) if user_session else {}
        # Merge defaults with user-specific overrides
        return {**DEFAULT_CONFIG, **user_settings}
    except Exception as e:
        print(f"Error fetching user settings: {str(e)}")
        return DEFAULT_CONFIG

def get_sem_unclassified_id(user_email: str) -> str:
    """Helper to get or create the sem_unclassified label ID."""
    res = create_label(user_email, UNCLASSIFIED_LABEL)
    return res.get("id")

def generate_category_name(snippets: list) -> str:
    """
    Uses Vertex AI (Gemini 3.5 Flash) to generate a concise category name.
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
    Workflow-Path-2 Orchestrator (Stable Fallback):
    1. Cooldown Guard: Prevents infinite reorg loops.
    2. Backlog Threshold: Only triggers if unclassified pile is significant.
    3. Adaptive Thresholds: Sets a realistic bar for each label.
    """
    try:
        # Load Dynamic Settings
        config = get_user_settings(user_email)
        
        client = get_client()
        db = client[settings.DB_NAME]
        email_collection = db["EmailData"]
        label_collection = db["LabelMetadata"]
        session_collection = db["UserSessions"]
        
        # --- 1. COOLDOWN GUARD ---
        session = session_collection.find_one({"user_email": user_email})
        if session and "last_reorganized_at" in session:
            last_reorg = datetime.fromisoformat(session["last_reorganized_at"])
            if datetime.now(UTC) - last_reorg < timedelta(hours=config["REORG_COOLDOWN_HOURS"]):
                print(f"[*] REORG COOLDOWN ACTIVE for {user_email}. Skipping reorganization.")
                return "Cooldown active. Skipping reorg."

        # --- 2. BACKLOG THRESHOLD ---
        # Only reorganize if we have a significant number of unclassified emails.
        unclassified_count = email_collection.count_documents({"user_email": user_email, "label": "unclassified"})
        
        project_id = os.getenv("PROJECT_ID", "grah-2026")
        vertexai.init(project=project_id, location="global")
        
        # 3. Get Current State
        gmail_labels = get_labels(user_email)
        active_gmail_sem_labels = [l["name"] for l in gmail_labels if l["name"].startswith("sem_") and l["name"] != UNCLASSIFIED_LABEL]
        unclassified_id = get_sem_unclassified_id(user_email)
        
        # Detect labels that need resetting
        db_labels = email_collection.distinct("label", {"user_email": user_email})
        orphaned_labels = [l for l in db_labels if l.startswith("sem_") and l not in active_gmail_sem_labels and l != UNCLASSIFIED_LABEL]
        
        all_sem_meta = list(label_collection.find({"user_email": user_email}))
        # Dynamic Check: Labels whose integrity is below their SPECIFIC adaptive threshold
        degraded_labels = [l["label_name"] for l in all_sem_meta if l["semantic_integrity_score"] < l.get("threshold_score", 0.7)]
        
        labels_to_shred = list(set(orphaned_labels + degraded_labels))

        # TRIGGER CONDITION: Heavy backlog OR degraded/orphaned labels OR system starting fresh
        if unclassified_count > config["BACKLOG_THRESHOLD"] or labels_to_shred or not active_gmail_sem_labels or len(active_gmail_sem_labels) > config["MAX_SEMANTIC_LABELS"]:
            print(f"[*] REORG STARTING for {user_email} (Backlog: {unclassified_count})...")

            if labels_to_shred:
                email_collection.update_many(
                    {"user_email": user_email, "label": {"$in": labels_to_shred}},
                    {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
                )
                label_collection.delete_many({"user_email": user_email, "label_name": {"$in": labels_to_shred}})
                
                if len(active_gmail_sem_labels) > config["MAX_SEMANTIC_LABELS"]:
                    for l_name in active_gmail_sem_labels:
                        l_id = next((l["id"] for l in gmail_labels if l["name"] == l_name), None)
                        if l_id: delete_label(user_email, l_id)

            # 3. Clustering (Capped at MAX_SEMANTIC_LABELS)
            email_count = email_collection.count_documents({"user_email": user_email, "label": "unclassified"})
            target_clusters = min(config["MAX_SEMANTIC_LABELS"], max(2, email_count // 5 if email_count < 25 else config["MAX_SEMANTIC_LABELS"]))
            
            clusters = cluster_unclassified_emails(user_email, n_clusters=target_clusters)
            if isinstance(clusters, str): return clusters
            
            for cluster in clusters:
                # 4. Generate AI Name
                snippets = [rep["snippet"] for rep in cluster["representatives"]]
                raw_name = generate_category_name(snippets)
                category_name = "sem_" + raw_name
                
                # 5. Create in Gmail
                new_label = create_label(user_email, category_name)
                label_id = new_label.get("id")
                if not label_id: continue
                
                # 6. Apply & Lock
                email_data_list = cluster["all_emails"]
                for item in email_data_list:
                    mongo_id = item["mongo_id"]
                    gmail_id = item["gmail_id"]
                    # DATA PRESERVATION: Only update the label. 
                    # thread_id and sent_at are preserved for Inbox-Analytics.
                    email_collection.update_one({"_id": ObjectId(mongo_id)}, {"$set": {"label": category_name}})
                    try:
                        apply_label_to_email(user_email, gmail_id, label_id)
                        if unclassified_id: remove_label_from_email(user_email, gmail_id, unclassified_id)
                    except: continue
                
                # 7. ADAPTIVE THRESHOLD SETTING
                # We calculate the starting integrity and set the threshold at a hysteresis % of that.
                meta_res = update_label_integrity(user_email, category_name)
                if isinstance(meta_res, dict):
                    achieved_score = meta_res["integrity_score"]
                    # Hysteresis: The bar is set lower than what we just achieved
                    adaptive_threshold = max(0.6, achieved_score * config["ADAPTIVE_THRESHOLD_HYSTERESIS"])
                    label_collection.update_one(
                        {"user_email": user_email, "label_name": category_name},
                        {"$set": {"threshold_score": adaptive_threshold}}
                    )

            # 8. UPDATE COOLDOWN
            session_collection.update_one(
                {"user_email": user_email},
                {"$set": {"last_reorganized_at": datetime.now(UTC).isoformat()}},
                upsert=True
            )
            return "Reorganization complete. System stabilized."
        
        return "Inbox is healthy."
        
    except Exception as e:
        print(f"Reorg Error: {str(e)}")
        return f"Error: {str(e)}"

def incremental_update_label_integrity(user_email: str, label_name: str, new_embedding: list):
    """
    Efficiently updates label integrity. 
    Uses ADAPTIVE THRESHOLDS to trigger reorg.
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
        
        new_vec = np.array(new_embedding)
        new_centroid = (old_centroid * n + new_vec) / (n + 1)
        new_score = calculate_cosine_similarity(new_vec, new_centroid)
        new_integrity = (old_avg * n + new_score) / (n + 1)
        
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

        # ADAPTIVE GUARD: Compare against the specific label's threshold
        threshold = meta.get("threshold_score", 0.75) # Default to 75%
        if new_integrity < threshold:
            print(f"LABEL DEGRADED: {label_name} ({new_integrity} < {threshold}). Triggering Disciplined Reorg...")
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
    Recalculates the centroid and integrity score for a specific label.
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
    Track 3: STABLE Batch Classifier.
    Prioritizes existing labels and uses a high-confidence Stability Gate.
    """
    try:
        # Load Dynamic Settings
        config = get_user_settings(user_email)
        
        label_collection = get_collection("LabelMetadata")
        email_collection = get_collection()
        
        unclassified = list(email_collection.find({"user_email": user_email, "label": "unclassified"}))
        if not unclassified:
            return "No unclassified emails to process."

        # 1. Sync with Gmail: Only match against labels that currently EXIST in the sidebar
        gmail_labels = get_labels(user_email)
        active_label_map = {l["name"]: l["id"] for l in gmail_labels if l["name"].startswith("sem_")}
        
        if not active_label_map:
            return "No active sem_ labels found in Gmail. Waiting for reorganization."

        # 2. Get metadata ONLY for these active labels
        labels_meta = list(label_collection.find({
            "user_email": user_email, 
            "label_name": {"$in": list(active_label_map.keys())}
        }))

        if not labels_meta:
            return "No metadata found for active labels."

        unclassified_id = get_sem_unclassified_id(user_email)
        match_count = 0
        
        for email in unclassified:
            new_vec = np.array(email["vector_embedding"])
            best_match_label = None
            highest_score = -1.0
            target_threshold = config["DEFAULT_SIMILARITY_THRESHOLD"]

            for lbl in labels_meta:
                centroid = np.array(lbl["centroid_vector"])
                score = calculate_cosine_similarity(new_vec, centroid)
                if score > highest_score:
                    highest_score = score
                    best_match_label = lbl["label_name"]
                    target_threshold = lbl.get("threshold_score", config["DEFAULT_SIMILARITY_THRESHOLD"])

            # 3. ADAPTIVE STABILITY GATE: Use the label's specific threshold_score or system default
            if highest_score > target_threshold and best_match_label in active_label_map:
                label_id = active_label_map[best_match_label]
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
        
        return f"Stable classification complete. Categorized {match_count} emails."

    except Exception as e:
        print(f"Stable Batch Classification Error: {str(e)}")
        return f"Error: {str(e)}"

def process_and_store_email(email_metadata: dict, email_body: str):
    """
    Coordinates embedding generation and storage.
    Now supports enriched metadata (thread_id, sent_at) for analytics.
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
    Workflow-2: Performs K-Means clustering.
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
