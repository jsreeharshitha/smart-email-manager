from datetime import datetime, UTC, timedelta
from tools.embedding_tool import generate_embedding
from db.mongo_client import get_collection, get_client
from tools.mongo_mcp import store_email_record, find_unclassified_by_semantic_group, setup_database
from tools.gmail_mcp import (
    create_label, 
    get_labels, 
    apply_label_to_email, 
    get_emails_by_id, 
    delete_label, 
    remove_label_from_email,
    get_gmail_service
)
import json
import os
import numpy as np
# ... (rest of imports unchanged)
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

def sync_label_lifecycle(user_email: str):
    """
    Periodic Cleanup Phase:
    Detects empty 'sem_' labels in Gmail and removes them from MongoDB.
    Enforces a 2-hour Maturity Lock to prevent premature deletion.
    """
    try:
        gmail_labels = get_labels(user_email)
        active_sem_labels = [l for l in gmail_labels if l["name"].startswith("sem_") and l["name"] != UNCLASSIFIED_LABEL]
        
        client = get_client()
        db = client[settings.DB_NAME]
        email_collection = db["EmailData"]
        label_collection = db["LabelMetadata"]

        # Identify empty categories
        stale_labels_info = [l for l in active_sem_labels if l.get("messagesTotal", 0) == 0]
        
        if stale_labels_info:
            stale_names = [l["name"] for l in stale_labels_info]
            
            # --- MATURITY LOCK CHECK ---
            # Only delete if label is > 2 hours old
            now = datetime.now(UTC)
            eligible_for_deletion = []
            for name in stale_names:
                meta = label_collection.find_one({"user_email": user_email, "label_name": name})
                if meta and "created_at" in meta:
                    created_at = datetime.fromisoformat(meta["created_at"])
                    if now - created_at > timedelta(hours=2):
                        eligible_for_deletion.append(name)
                elif not meta:
                    # If no metadata exists, it's an untracked label, safe to delete
                    eligible_for_deletion.append(name)

            if eligible_for_deletion:
                print(f"[*] CLEANUP: Deleting {len(eligible_for_deletion)} mature empty labels: {eligible_for_deletion}")
                email_collection.update_many(
                    {"user_email": user_email, "label": {"$in": eligible_for_deletion}},
                    {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
                )
                label_collection.delete_many({"user_email": user_email, "label_name": {"$in": eligible_for_deletion}})
                
                for l in stale_labels_info:
                    if l["name"] in eligible_for_deletion:
                        try:
                            delete_label(user_email, l["id"])
                        except: continue

        # --- GHOST EMAIL RECOVERY ---
        # ... (rest of function remains same)
        # Detect emails in MongoDB that claim an sem_ label but are NOT in the active Gmail list.
        # This fixes the drift where MongoDB thinks an email is organized but Gmail does not.
        db_sem_labels = email_collection.distinct("label", {"user_email": user_email})
        ghost_labels = [l for l in db_sem_labels if l.startswith("sem_") and l not in [lx["name"] for lx in gmail_labels] and l != UNCLASSIFIED_LABEL]
        
        if ghost_labels:
            print(f"[*] RECOVERY: Found {len(ghost_labels)} ghost labels in DB. Resetting emails...")
            email_collection.update_many(
                {"user_email": user_email, "label": {"$in": ghost_labels}},
                {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
            )
            
        return len(stale_labels) + len(ghost_labels)
    except Exception as e:
        print(f"Label Lifecycle Sync Error: {str(e)}")
        return 0

def demolish_weak_labels(user_email: str):
    """
    Workflow-Path-2 Orchestrator (The Demolisher):
    1. Cooldown Guard: Prevents infinite loops.
    2. Cleanup: Identifies and shreds weak/degraded or orphaned labels.
    3. Trigger: Executes only if active labels > MAX_LABELS or degraded labels exist.
    """
    try:
        # --- PRE-REORG CLEANUP ---
        sync_label_lifecycle(user_email)

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
                print(f"[*] DEMOLISH COOLDOWN ACTIVE for {user_email}. Skipping.")
                return "Cooldown active. Skipping demolish."

        # --- 2. STATE CHECK ---
        try:
            gmail_labels = get_labels(user_email)
        except Exception as e:
            print(f"[!] ABORTING DEMOLISH: Label sync failed: {str(e)}")
            return f"Error: Label sync failed. preserving state."

        active_gmail_sem_labels = [l["name"] for l in gmail_labels if l["name"].startswith("sem_") and l["name"] != UNCLASSIFIED_LABEL]
        num_active_labels = len(active_gmail_sem_labels)
        max_labels = config["MAX_SEMANTIC_LABELS"]
        
        # SAFETY GATE
        db_sem_label_count = label_collection.count_documents({"user_email": user_email})
        if not active_gmail_sem_labels and db_sem_label_count > 0:
            print(f"[!] SAFETY TRIGGERED: Gmail returned 0 sem_ labels but DB has {db_sem_label_count}. Aborting demolish.")
            return "Safety Gate: Aborted due to suspicious empty label list."
        
        # Detect labels that need resetting
        db_labels = email_collection.distinct("label", {"user_email": user_email})
        orphaned_labels = [l for l in db_labels if l.startswith("sem_") and l not in active_gmail_sem_labels and l != UNCLASSIFIED_LABEL]
        
        all_sem_meta = list(label_collection.find({"user_email": user_email}))
        now = datetime.now(UTC)
        
        labels_to_shred = []
        for l in all_sem_meta:
            name = l["label_name"]
            integrity = l["semantic_integrity_score"]
            threshold = l.get("threshold_score", 0.7)
            
            # --- MATURITY LOCK & BIRTH THRESHOLD ---
            is_mature = False
            if "created_at" in l:
                created_at = datetime.fromisoformat(l["created_at"])
                if now - created_at > timedelta(hours=2):
                    is_mature = True
            
            # Shred Condition 1: Label is mature AND degraded
            if is_mature and integrity < threshold:
                labels_to_shred.append(name)
                continue
                
            # Shred Condition 2: Label is a ghost/orphan
            if name in orphaned_labels:
                labels_to_shred.append(name)
                continue
                
            # Shred Condition 3 (Advanced): Even if not mature, shred if it crashes below a panic level 
            # (e.g. 10% below its own threshold)
            if not is_mature and integrity < (threshold * 0.9):
                print(f"[*] PANIC SHRED: {name} ({integrity}) crashed below birth-safety. Shredding early.")
                labels_to_shred.append(name)

        # --- 3. EXECUTION TRIGGER ---
        should_execute = (
            num_active_labels > max_labels or 
            len(labels_to_shred) > 0
        )

        if should_execute:
            print(f"[*] DEMOLISH PHASE STARTING for {user_email}. Active labels: {num_active_labels}")

            if labels_to_shred:
                print(f"[*] Shredding weak/orphaned labels: {labels_to_shred}")
                email_collection.update_many(
                    {"user_email": user_email, "label": {"$in": labels_to_shred}},
                    {"$set": {"label": "unclassified", "email_semantic_score": 0.0}}
                )
                label_collection.delete_many({"user_email": user_email, "label_name": {"$in": labels_to_shred}})
                
                # Cleanup Gmail if we are over the absolute maximum
                if num_active_labels > max_labels:
                    for l_name in labels_to_shred:
                        l_id = next((l["id"] for l in gmail_labels if l["name"] == l_name), None)
                        if l_id: delete_label(user_email, l_id)

            # --- 4. LOCK ---
            session_collection.update_one(
                {"user_email": user_email},
                {"$set": {"last_reorganized_at": datetime.now(UTC).isoformat()}},
                upsert=True
            )
            return "Demolish complete. Weak labels removed."
        
        return "Inbox is healthy. No demolition needed."
        
    except Exception as e:
        print(f"Demolish Error: {str(e)}")
        import traceback
        traceback.print_exc()
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
                },
                "$setOnInsert": {
                    "created_at": datetime.now(UTC).isoformat()
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

def sync_by_time_range(user_email: str, minutes: int = 30):
    """
    Fetches and processes emails received within the last N minutes.
    Memory-efficient fallback for large history gaps.
    """
    try:
        service = get_gmail_service(user_email)
        
        # Calculate timestamp for Gmail query (seconds)
        after_timestamp = int((datetime.now(UTC) - timedelta(minutes=minutes)).timestamp())
        query = f"after:{after_timestamp}"
        
        print(f"[*] QUICK SYNC: Fetching emails since {minutes} mins ago (Query: {query})")
        
        results = service.users().messages().list(userId='me', q=query, maxResults=50).execute()
        messages = results.get('messages', [])
        
        processed_count = 0
        for msg in messages:
            msg_id = msg['id']
            try:
                msg_detail = service.users().messages().get(userId='me', id=msg_id).execute()
                metadata = {
                    "subject": next((h['value'] for h in msg_detail.get('payload', {}).get('headers', []) if h['name'] == 'Subject'), "No Subject"),
                    "message_id": msg_id,
                    "user_email": user_email
                }
                process_and_store_email(metadata, msg_detail.get('snippet', ''))
                processed_count += 1
            except Exception as e:
                print(f"Error processing {msg_id} during quick sync: {str(e)}")
                continue
            
        print(f"[+] QUICK SYNC COMPLETE: Processed {processed_count} emails.")
        return processed_count
    except Exception as e:
        print(f"Quick Sync Error: {str(e)}")
        return 0

def sync_unclassified_integrity(user_email: str):
    """
    Self-Healing: Ensures all 'unclassified' emails in MongoDB are labeled 'sem_unclassified' in Gmail.
    Fixes the drift caused by transient initial intake failures.
    """
    try:
        from tools.gmail_mcp import list_messages_in_label, batch_modify_emails
        
        # 1. Get MongoDB unclassified IDs
        client = get_client()
        db = client[settings.DB_NAME]
        email_collection = db["EmailData"]
        mongo_unclassified_ids = email_collection.distinct("message_id", {"user_email": user_email, "label": "unclassified"})
        
        if not mongo_unclassified_ids:
            return 0
            
        # 2. Get Gmail sem_unclassified IDs
        gmail_unclassified_ids = list_messages_in_label(user_email, UNCLASSIFIED_LABEL)
        
        # 3. Find the Delta: In DB but NOT in Gmail label
        missing_ids = list(set(mongo_unclassified_ids) - set(gmail_unclassified_ids))
        
        if missing_ids:
            print(f"[*] INTEGRITY SYNC: Found {len(missing_ids)} emails missing 'sem_unclassified' label in Gmail. Fixing...")
            unclassified_id = get_sem_unclassified_id(user_email)
            if unclassified_id:
                # Use batch modify for efficiency
                batch_modify_emails(user_email, missing_ids, add_label_ids=[unclassified_id])
                print(f"[+] Fixed {len(missing_ids)} emails in Gmail.")
        
        return len(missing_ids)
    except Exception as e:
        print(f"Unclassified Integrity Error: {str(e)}")
        return 0

def perform_batch_classification(user_email: str):
    """
    Track 3: STABLE Batch Classifier.
    Prioritizes existing labels and uses a high-confidence Stability Gate.
    Enhanced with Long-Term Memory (UserPreferences).
    """
    try:
        # --- PRE-CLASSIFICATION CLEANUP & INTEGRITY ---
        sync_label_lifecycle(user_email)
        sync_unclassified_integrity(user_email)

        from tools.mongo_mcp import search_user_preferences
        
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
            print("[*] No active sem_ labels found in Gmail. Handing off to Architect for initialization.")
            return build_strong_labels(user_email)

        # 2. Get metadata ONLY for these active labels
        labels_meta = list(label_collection.find({
            "user_email": user_email, 
            "label_name": {"$in": list(active_label_map.keys())}
        }))

        if not labels_meta:
            print("[*] No metadata found for active labels. Handing off to Architect for recovery.")
            return build_strong_labels(user_email)

        unclassified_id = get_sem_unclassified_id(user_email)
        match_count = 0
        
        # Initialize Gemini for Memory Reconciliation (if needed)
        project_id = os.getenv("PROJECT_ID", "grah-2026")
        vertexai.init(project=project_id, location="global")
        model = GenerativeModel("gemini-3.5-flash")

        for email in unclassified:
            new_vec = np.array(email["vector_embedding"])
            best_match_label = None
            highest_score = -1.0
            target_threshold = config["DEFAULT_SIMILARITY_THRESHOLD"]

            # --- 2a. Standard Centroid Search ---
            for lbl in labels_meta:
                centroid = np.array(lbl["centroid_vector"])
                score = calculate_cosine_similarity(new_vec, centroid)
                if score > highest_score:
                    highest_score = score
                    best_match_label = lbl["label_name"]
                    # Pattern A Relaxation: Easier to join degraded labels
                    target_threshold = min(lbl.get("threshold_score", config["DEFAULT_SIMILARITY_THRESHOLD"]), config["DEFAULT_SIMILARITY_THRESHOLD"])

            # --- 2b. Long-Term Memory Search (Context Enrichment) ---
            memory_json = search_user_preferences(user_email, email["vector_embedding"])
            memories = json.loads(memory_json)
            best_memory = memories[0] if memories and memories[0].get("score", 0) > 0.8 else None

            # --- 3. ADAPTIVE STABILITY GATE & MEMORY RECONCILIATION ---
            final_label = best_match_label
            final_score = highest_score
            should_apply = highest_score > target_threshold

            if best_memory and best_memory.get("confidence_score", 0) > 0.8:
                # Use Gemini to reconcile centroid match with semantic memory
                prompt = f"""
                You are a Personal Email Assistant. Decide if this email should be categorized based on a learned preference.
                
                EMAIL SNIPPET: {email.get('snippet')}
                CENTROID MATCH: {best_match_label} (Score: {highest_score:.2f})
                LEARNED PREFERENCE: {best_memory.get('llm_semantic_note')}
                PREFERRED ACTION: {best_memory.get('structured_rule', {}).get('preferred_action')}
                
                If the preference is strong and applies, return the preferred action or confirm the label.
                Return ONLY a JSON: {{"action": "archive|label", "label": "label_name", "reason": "..."}}
                """
                try:
                    res = model.generate_content(prompt)
                    decision = json.loads(res.text.strip().replace("```json", "").replace("```", ""))
                    if decision.get("action") == "archive":
                        # Logic for archive could be added here
                        pass 
                    if decision.get("label") and decision["label"] in active_label_map:
                        final_label = decision["label"]
                        should_apply = True
                        print(f"[*] MEMORY OVERRIDE: {email.get('message_id')} categorized as {final_label}")
                except:
                    pass

            if should_apply and final_label in active_label_map:
                label_id = active_label_map[final_label]
                gmail_id = email.get("message_id")
                
                email_collection.update_one(
                    {"_id": email["_id"]},
                    {"$set": {"label": final_label, "email_semantic_score": float(final_score)}}
                )
                try:
                    apply_label_to_email(user_email, gmail_id, label_id)
                    if unclassified_id:
                        remove_label_from_email(user_email, gmail_id, unclassified_id)
                    incremental_update_label_integrity(user_email, final_label, email["vector_embedding"])
                    match_count += 1
                except: continue
        
        res_msg = f"Stable classification complete. Categorized {match_count} emails."
        
        # --- 4. HANDOFF TO ARCHITECT ---
        remaining_unclassified = email_collection.count_documents({"user_email": user_email, "label": "unclassified"})
        if remaining_unclassified > config["BACKLOG_THRESHOLD"]:
            print(f"[*] Batch Classifier handing off to Architect. Backlog: {remaining_unclassified}")
            return res_msg + " " + build_strong_labels(user_email)
            
        return res_msg

    except Exception as e:
        print(f"Stable Batch Classification Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}"

def build_strong_labels(user_email: str):
    """
    Workflow-Path-3 Orchestrator (The Architect):
    Creates new semantic categories when the backlog gets too large.
    Enforces a 30-minute lock to prevent concurrent build bursts.
    """
    try:
        config = get_user_settings(user_email)
        client = get_client()
        db = client[settings.DB_NAME]
        email_collection = db["EmailData"]
        label_collection = db["LabelMetadata"]
        session_collection = db["UserSessions"]

        # --- ARCHITECT COOLDOWN ---
        session = session_collection.find_one({"user_email": user_email})
        if session and "last_built_at" in session:
            last_build = datetime.fromisoformat(session["last_built_at"])
            if datetime.now(UTC) - last_build < timedelta(minutes=30):
                print(f"[*] ARCHITECT COOLDOWN ACTIVE for {user_email}. Skipping.")
                return "Architect Cooldown active."

        try:
            gmail_labels = get_labels(user_email)
        except: 
            return "Builder: Gmail sync failed."
        
        active_gmail_sem_labels = [l["name"] for l in gmail_labels if l["name"].startswith("sem_") and l["name"] != UNCLASSIFIED_LABEL]
        unclassified_count = email_collection.count_documents({"user_email": user_email, "label": "unclassified"})
        
        # GUARDS
        if len(active_gmail_sem_labels) > config["MAX_SEMANTIC_LABELS"] + 2:
            return "Builder Guard: Too many active labels. Waiting for demolisher."
        if unclassified_count <= config["BACKLOG_THRESHOLD"]:
            return "Builder Guard: Backlog resolved by classifier."
            
        print(f"[*] BUILDER PHASE STARTING for {user_email}. Generating new categories...")

        # Update Last Built timestamp immediately to act as a lock
        session_collection.update_one(
            {"user_email": user_email},
            {"$set": {"last_built_at": datetime.now(UTC).isoformat()}},
            upsert=True
        )
        
        unclassified_id = get_sem_unclassified_id(user_email)
        
        # Calculate target clusters
        target_clusters = min(config["MAX_SEMANTIC_LABELS"], max(2, unclassified_count // 5 if unclassified_count < 25 else config["MAX_SEMANTIC_LABELS"]))
        
        clusters = cluster_unclassified_emails(user_email, n_clusters=target_clusters)
        if isinstance(clusters, str): return clusters
        
        created_count = 0
        for cluster in clusters:
            if cluster["count"] < 2: continue
            
            snippets = [rep["snippet"] for rep in cluster["representatives"]]
            raw_name = generate_category_name(snippets)
            category_name = "sem_" + raw_name
            
            new_label = create_label(user_email, category_name)
            label_id = new_label.get("id")
            if not label_id: continue
            
            email_data_list = cluster["all_emails"]
            gmail_ids = [item["gmail_id"] for item in email_data_list]
            
            try:
                from tools.gmail_mcp import batch_modify_emails
                remove_ids = [unclassified_id] if unclassified_id else []
                batch_res = batch_modify_emails(user_email, gmail_ids, add_label_ids=[label_id], remove_label_ids=remove_ids)
                
                if "Successfully" in batch_res:
                    for item in email_data_list:
                        email_collection.update_one({"_id": ObjectId(item["mongo_id"])}, {"$set": {"label": category_name}})
                    
                    # Initialize Hysteresis Threshold
                    update_label_integrity(user_email, category_name)
                    meta = label_collection.find_one({"user_email": user_email, "label_name": category_name})
                    if meta:
                        achieved_score = meta.get("semantic_integrity_score", 0.85)
                        adaptive_threshold = max(0.6, achieved_score * config["ADAPTIVE_THRESHOLD_HYSTERESIS"])
                        label_collection.update_one(
                            {"user_email": user_email, "label_name": category_name},
                            {"$set": {"threshold_score": adaptive_threshold}}
                        )
                    created_count += 1
                    print(f"[+] Architect built {category_name} with {len(gmail_ids)} emails.")
            except Exception as e:
                print(f"Builder Batch Error: {str(e)}")
                continue
                
        return f"Architect built {created_count} new categories."
        
    except Exception as e:
        print(f"Builder Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return f"Builder Error: {str(e)}"

def process_and_store_email(email_metadata: dict, email_body: str):
    """
    Coordinates embedding generation and storage.
    Now implements Dual-Write: Voyage AI (Legacy) + Vertex AI (BigQuery Search).
    """
    from tools.embedding_tool import generate_embedding, generate_vertex_embedding
    
    # 1. Generate both embeddings
    voyage_embedding = generate_embedding(email_body)
    vertex_embedding = generate_vertex_embedding(email_body)

    document = {
        **email_metadata,
        "vector_embedding": voyage_embedding, # Primary for MongoDB search
        "vertex_embedding": vertex_embedding, # Target for BigQuery search
        "label": "unclassified",
        "email_semantic_score": 0.0,
        "processed_at": datetime.now(UTC).isoformat(),
        "arrival_at": datetime.now(UTC).isoformat(), 
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
