# SEM Orchestration Plan

This document outlines the structured execution plan for the Smart Email Manager (SEM) agent, dividing the orchestration into three distinct, self-balancing phases: Demolish, Classify, and Build.

## 1. `demolish_weak_labels(user_email)`
**Role:** The Cleanup Crew.

**Triggers (OR conditions):**
1. Active `sem_` labels in Gmail > `MAX_SEMANTIC_LABELS` (5). *(Keeps the sidebar clean).*
2. Pub/Sub Notification: Triggered when `incremental_update_label_integrity` detects a score drop below the label's specific threshold.

**Action:**
*   Identifies orphaned labels (exist in MongoDB but not Gmail) and degraded labels (integrity score < threshold).
*   Shreds them: Deletes the Gmail labels, removes metadata from MongoDB, and resets the associated emails back to `unclassified`.

**Guard:**
*   Enforces a 1-hour `REORG_COOLDOWN` to prevent continuous thrashing.

## 2. `perform_batch_classification(user_email)`
**Role:** The Sorter.

**Trigger:**
*   Runs frequently (e.g., every 10 incoming emails).

**Action:**
*   Iterates through the `unclassified` pile and attempts to fit them into *existing* active labels.

**Logic Update (Pattern A Relaxation):**
*   An email can enter a category if its similarity to the centroid is greater than `min(threshold_score, DEFAULT_SIMILARITY_THRESHOLD)`.
*   *Effect:* If a category naturally degrades (e.g., threshold drops to 0.70), it becomes slightly easier for emails to join it, right up until the Demolisher is triggered to shred it entirely.

**Post-Action Handoff:**
*   If, after sorting everything possible, the remaining `unclassified` pile is still > `BACKLOG_THRESHOLD` (15), it **triggers `build_strong_labels()`**.

## 3. `build_strong_labels(user_email)`
**Role:** The Architect.

**Trigger:**
*   Called explicitly by the Batch Classifier when the unclassified backlog remains too high.
*   **0-Label Auto-Trigger:** Also triggered if the system detects 0 active `sem_` labels while the unclassified backlog is > `BACKLOG_THRESHOLD`.

**Guards (AND conditions):**
1. Current active `sem_` labels <= `MAX_SEMANTIC_LABELS + 2` (7). *(Prevents infinite creation if the demolisher is on cooldown).*
2. `unclassified` count > `BACKLOG_THRESHOLD` (15).

**Action:**
*   Runs KMeans clustering on the `unclassified` pile.
*   Calls Gemini to generate semantic names for the new clusters.
*   Creates new `sem_` labels in Gmail.
*   Uses `batch_modify_emails` to assign the emails efficiently.
*   Initializes the `threshold_score` (Hysteresis) in MongoDB for the newly created labels.

---

### The Lifecycle Loop
1.  **Incoming Mail** -> `perform_batch_classification` tries to sort it.
2.  If sorting fails (too many new topics) -> Backlog hits 15.
3.  **Backlog hits 15** -> `build_strong_labels` creates new categories for the new topics.
4.  If it creates too many categories (>5) or categories get too diluted (score drops) -> `demolish_weak_labels` shreds the worst ones, pushing them back to step 1.
