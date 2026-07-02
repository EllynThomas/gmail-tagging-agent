"""
Step 2: Classify and label emails.

Fetches recent inbox emails that haven't been processed yet (i.e. don't
already have the HBF label), sends each one (subject + sender + snippet)
to Claude for classification, applies the matching Gmail labels, then
tags the email with HBF so future runs skip it.

This does NOT archive or delete anything -- it only adds labels, so it's
safe to run repeatedly.

Requires ANTHROPIC_API_KEY to be set as an environment variable.
"""

import json
import os.path
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import anthropic

# Modify scope for reading/labeling, send scope for the weekly review
# script, and settings.basic for creating Gmail filters.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

# How many recent inbox emails to process per run.
MAX_RESULTS = 20

# Gmail's ACTUAL built-in category tabs (these are the only valid ones --
# "Purchases" and "Bills" are not real Gmail category IDs, despite showing
# up as sub-groupings in the Gmail UI sometimes).
CATEGORY_LABELS = [
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
    "CATEGORY_PROMOTIONS",
]

# Your custom labels and what belongs in each. Edit these descriptions
# any time you notice the classifier getting something wrong.
CUSTOM_LABELS = {
    "Job/Board": "Job alerts or job listings, e.g. from LinkedIn, Indeed, job boards.",
    "Job/Replies": "Responses from a recruiter or employer about an application you submitted.",
    "Job/Sent Applications": "Confirmation copies of job applications you personally sent out.",
    "Notes": "Casual messages from the user themself or a friend, usually with no real subject line.",
    "Payslips": "The user's own salary slips/payslips from their employer.",
    "Receipts": "Purchase confirmations, invoices, order receipts in general.",
    "Receipts/Lime": "Receipts specifically from Lime (e.g. Lime scooter/bike rides).",
    "Tickets": "Travel tickets (flights, trains) or event tickets.",
    "Uni": "University-related emails (courses, admin, professors, deadlines).",
    "PM/Fashion": "Marketing emails from clothing, shoe, or fashion brands/retailers.",
    "PM/Travel": "Marketing emails about flights, hotels, travel deals.",
    "PM/Events": "Marketing emails about events, concerts, gigs, things happening on a date.",
    "PM/Other": "Marketing/promotional emails that don't fit Fashion, Travel, or Events.",
    "Dracula": "Emails from a serialized email subscription that sends chapters of Dracula.",
    "Junk": "Spam-adjacent or low-value unwanted email that Gmail's own Spam filter didn't catch.",
    "News": "News articles or newsletters from news outlets/journalists (e.g. BBC), not general notifications.",
    "Courses": "Online courses or learning platforms (e.g. Coursera, Udemy) -- NOT university-related (that's Uni).",
    "Security/Alerts": "Security, login, or identity-verification alerts from services (e.g. 'new sign-in', verification codes, account activity notices).",
    "PM/Newsletters": "Non-promotional newsletter content from causes, organisations, interest groups, or publications -- not trying to sell anything, so distinct from other PM/* labels.",
    "PM/Gaming": "Promotional or update emails related to games, puzzles, or gaming platforms/communities (e.g. Patreon posts from puzzle creators, game studio newsletters).",
    "Receipts/Travel": "Receipts specifically for travel-related purchases (flights, accommodation, travel bookings) -- a more specific version of Receipts.",
    "Job/Alerts": "Recruiter or job-platform outreach emails curating/recommending specific roles to you -- distinct from Job/Board, which is raw job listing digests.",
    "Subscriptions": "Newsletter or service emails from things you're subscribed to that aren't security-sensitive and don't need active review (e.g. Substack newsletters, platform notifications, trip reminders).",
}

FALLBACK_LABEL = "Needs Review"

# Marker label applied to every email once it's been processed, so future
# runs skip it instead of reclassifying the same emails repeatedly.
PROCESSED_LABEL = "HBF"

# Labels that are safe to strip and reapply each run. Deliberately excludes
# system/state labels (INBOX, UNREAD, SPAM, TRASH, HBF) since removing those
# would change something other than classification (e.g. archiving).
REMOVABLE_LABEL_NAMES = list(CUSTOM_LABELS.keys()) + [FALLBACK_LABEL]

client = anthropic.Anthropic()


def get_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def build_batch_classification_prompt(emails):
    custom_label_list = "\n".join(
        f"- {name}: {desc}" for name, desc in CUSTOM_LABELS.items()
    )

    emails_block = "\n\n".join(
        f"EMAIL {i}:\nFrom: {e['sender']}\nSubject: {e['subject']}\nSnippet: {e['snippet']}"
        for i, e in enumerate(emails)
    )

    return f"""You are sorting emails into labels for a Gmail inbox. Below are
{len(emails)} emails, each numbered. Classify EACH ONE independently.

{emails_block}

For each email, choose ONE Gmail category tab from this list (or null if none fit well):
Personal, Social, Updates, Forums, Promotions

Also choose ZERO OR MORE custom labels from this list:
{custom_label_list}

Note: purchase receipts and bills should go in the "Receipts" or "Payslips"
custom labels, not the category tab -- there is no separate category tab
for purchases or bills.

Note: the "Promotions" category tab is separate from the PM/* custom
labels (PM/Fashion, PM/Travel, PM/Events, PM/Other, PM/Newsletters) --
both can apply to the same marketing email. Use PM/Fashion, PM/Travel,
or PM/Events if it clearly fits; use PM/Newsletters if it's non-promotional
newsletter content (causes, organisations, interest groups -- not trying
to sell something); otherwise if it's still marketing/promotional, use
PM/Other.

Note: Job/Board is for raw job listing digests/alerts (e.g. LinkedIn Job
Alerts, Indeed digests). Job/Alerts is for a recruiter or job platform
personally curating/recommending specific roles to you (e.g. "21 new
roles match your profile", a recruiter reaching out about a specific job).

Note: Subscriptions is for newsletter/service emails you're subscribed to
that are not security-sensitive and don't need active review (e.g.
Substack newsletters, platform activity notifications, trip reminders).
Security/Alerts is specifically for security, login, or identity
verification emails (sign-in alerts, verification codes, account activity
notices) -- these should NEVER go to Subscriptions even if from a
service you're subscribed to, since they may need attention.

If nothing fits clearly into any custom label, use "Needs Review" as the only custom label.

Respond with ONLY a valid JSON array, no other text, with exactly {len(emails)}
objects in the SAME ORDER as the emails above, in this exact format:
[{{"category": "Updates" or null, "custom_labels": ["Job/Board"]}}, ...]"""


def classify_emails_batch(emails):
    """Classify a list of emails (each a dict with sender/subject/snippet)
    in a single API call. Returns a list of result dicts in the same order,
    or falls back to "Needs Review" for every email if parsing fails."""
    prompt = build_batch_classification_prompt(emails)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200 * len(emails),
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text.strip()
    # Defensive cleanup in case the model wraps the JSON in code fences.
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()

    try:
        results = json.loads(raw_text)
        if not isinstance(results, list) or len(results) != len(emails):
            raise ValueError(
                f"Expected a list of {len(emails)} results, got {results!r}"
            )
        return results
    except (json.JSONDecodeError, ValueError) as error:
        print(f"  [!] Could not parse batch classification response: {error}")
        print(f"  [!] Raw response was: {raw_text[:500]}")
        return [
            {"category": None, "custom_labels": [FALLBACK_LABEL]}
            for _ in emails
        ]


def get_or_create_label_id(service, label_cache, label_name):
    """Look up a label's ID by name (case-insensitive), creating it if it
    doesn't exist yet. Handles the case where Gmail reports a conflict on
    creation even though our local name-matching didn't find it first."""
    cache_key = label_name.lower()
    if cache_key in label_cache:
        return label_cache[cache_key]

    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"].lower() == cache_key:
            label_cache[cache_key] = label["id"]
            return label["id"]

    # Label doesn't exist locally -- try to create it. This handles nested
    # labels like "Job/Board" automatically (Gmail creates the parent if
    # needed).
    try:
        new_label = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        label_cache[cache_key] = new_label["id"]
        return new_label["id"]
    except HttpError as error:
        if error.resp.status == 409:
            # Gmail says it already exists under some name/casing we missed.
            # Re-fetch the label list fresh and try again to find it.
            results = service.users().labels().list(userId="me").execute()
            for label in results.get("labels", []):
                if label["name"].lower() == cache_key:
                    label_cache[cache_key] = label["id"]
                    return label["id"]
            # Still not found -- re-raise with more context since this is
            # an unusual case worth seeing in full.
            print(f"  [!] Gmail reports '{label_name}' conflicts but it "
                  f"wasn't found in the label list. Skipping this label.")
            return None
        raise


def get_removable_label_ids(service, label_cache, message_label_ids):
    """Given a message's current label IDs, return the subset that are
    classification labels we manage (categories + custom labels), so they
    can be stripped before re-applying fresh ones. System/state labels
    (INBOX, UNREAD, SPAM, TRASH, HBF) are never included here."""
    removable_ids = set()

    for cat_name in CATEGORY_LABELS:
        if cat_name in message_label_ids:
            removable_ids.add(cat_name)

    for label_name in REMOVABLE_LABEL_NAMES:
        label_id = get_or_create_label_id(service, label_cache, label_name)
        if label_id and label_id in message_label_ids:
            removable_ids.add(label_id)

    return list(removable_ids)


def main():
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    # Never touch emails older than this -- keeps the script from reaching
    # into old backlog you may have already handled manually.
    one_month_ago = datetime.now() - timedelta(days=30)
    date_cutoff = one_month_ago.strftime("%Y/%m/%d")

    results = (
        service.users()
        .messages()
        .list(
            userId="me",
            labelIds=["INBOX"],
            q=f"-label:{PROCESSED_LABEL} after:{date_cutoff}",
            maxResults=MAX_RESULTS,
        )
        .execute()
    )
    messages = results.get("messages", [])

    if not messages:
        print("No messages found.")
        return

    print(f"Processing {len(messages)} messages...\n")

    label_cache = {}

    # Phase 1: fetch metadata for every message up front.
    email_data = []
    for msg_ref in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_ref["id"], format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )

        headers = msg["payload"]["headers"]
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "(unknown sender)")
        snippet = msg.get("snippet", "")
        current_label_ids = msg.get("labelIds", [])

        email_data.append({
            "id": msg_ref["id"],
            "sender": sender,
            "subject": subject,
            "snippet": snippet,
            "current_label_ids": current_label_ids,
        })

    # Phase 2: classify all of them in a single batched API call.
    print(f"Classifying {len(email_data)} emails in one batch call...\n")
    classifications = classify_emails_batch(email_data)

    # Phase 3: apply labels for each email based on its classification.
    for email, result in zip(email_data, classifications):
        category = result.get("category")
        custom_labels = result.get("custom_labels", [])

        print(f"From:    {email['sender']}")
        print(f"Subject: {email['subject']}")
        print(f"  -> Category: {category}")
        print(f"  -> Labels:   {custom_labels}")

        # Strip any previously-applied categories/custom labels before
        # adding the fresh set, so the classifier's current call is the
        # single source of truth (HBF and other system labels untouched).
        label_ids_to_remove = get_removable_label_ids(
            service, label_cache, email["current_label_ids"]
        )

        label_ids_to_add = []

        if category:
            gmail_category_name = f"CATEGORY_{category.upper()}"
            if gmail_category_name in CATEGORY_LABELS:
                label_ids_to_add.append(gmail_category_name)

        for label_name in custom_labels:
            label_id = get_or_create_label_id(service, label_cache, label_name)
            if label_id:
                label_ids_to_add.append(label_id)

        # Always tag with HBF so this email is skipped on future runs.
        hbf_id = get_or_create_label_id(service, label_cache, PROCESSED_LABEL)
        if hbf_id:
            label_ids_to_add.append(hbf_id)

        # Don't remove a label we're about to re-add in the same call.
        label_ids_to_remove = [
            lid for lid in label_ids_to_remove if lid not in label_ids_to_add
        ]

        if label_ids_to_add or label_ids_to_remove:
            body = {}
            if label_ids_to_add:
                body["addLabelIds"] = label_ids_to_add
            if label_ids_to_remove:
                body["removeLabelIds"] = label_ids_to_remove

            service.users().messages().modify(
                userId="me",
                id=email["id"],
                body=body,
            ).execute()

        print("-" * 50)

    print("\nDone.")


if __name__ == "__main__":
    main()
