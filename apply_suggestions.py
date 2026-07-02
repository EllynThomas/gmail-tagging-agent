"""
Apply approved suggestions from the most recent weekly inbox review email.

Run on Tuesday and Friday after weekly_review.py sends its email.
The user replies to the review email with the suggestion numbers they want
(e.g. "1, 3" or "all" or "all except 2").

On each run:
- Finds the most recent un-applied weekly review email.
- Checks the thread for a reply from the user.
- If no reply yet: exits silently (will try again on the next scheduled run).
- If reply found: applies the approved labels, filters, and retroactive
  relabels, marks the review email with the "Review/Applied" label so a
  second run won't double-apply, and sends a confirmation reply into the thread.
"""

import base64
import json
import os.path
from datetime import datetime
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import anthropic

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

REVIEW_SUBJECT_PREFIX = "Weekly inbox review --"
APPLIED_LABEL = "Review/Applied"
JSON_START_MARKER = "--- BEGIN SUGGESTIONS JSON (do not modify) ---"
JSON_END_MARKER = "--- END SUGGESTIONS JSON ---"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

client = anthropic.Anthropic()


def get_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return creds


def get_or_create_label_id(service, label_cache, label_name):
    cache_key = label_name.lower()
    if cache_key in label_cache:
        return label_cache[cache_key]

    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"].lower() == cache_key:
            label_cache[cache_key] = label["id"]
            return label["id"]

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
            results = service.users().labels().list(userId="me").execute()
            for label in results.get("labels", []):
                if label["name"].lower() == cache_key:
                    label_cache[cache_key] = label["id"]
                    return label["id"]
            print(f"  [!] '{label_name}' conflicts but wasn't found. Skipping.")
            return None
        raise


def _decode_body(payload):
    """Extract plain text body from a Gmail message payload."""
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part["mimeType"] == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    return ""


def find_latest_unapplied_review(service):
    """Find the most recent weekly review email not yet marked Review/Applied.
    Returns (message_id, thread_id, body_text) or (None, None, None).

    Fetches up to 10 candidates because Gmail's subject: search also matches
    reply threads (whose subject starts with 'Re:'), so we skip those and take
    the first original."""
    results = service.users().messages().list(
        userId="me",
        q=f'subject:"{REVIEW_SUBJECT_PREFIX}" -label:"{APPLIED_LABEL}"',
        maxResults=10,
    ).execute()
    messages = results.get("messages", [])
    if not messages:
        return None, None, None

    for candidate in messages:
        msg_id = candidate["id"]
        meta = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["Subject"],
        ).execute()
        subject = next(
            (h["value"] for h in meta["payload"]["headers"] if h["name"].lower() == "subject"),
            "",
        )
        if subject.startswith(REVIEW_SUBJECT_PREFIX):
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
            return msg_id, msg["threadId"], _decode_body(msg["payload"])

    return None, None, None


def find_user_reply(service, thread_id, original_id, my_email):
    """Return the body of the user's reply in this thread, or None if not found."""
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()
    for msg in thread.get("messages", []):
        if msg["id"] == original_id:
            continue
        headers = msg["payload"]["headers"]
        from_header = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
        if my_email.lower() in from_header.lower():
            return _decode_body(msg["payload"])
    return None


def extract_suggestions_json(email_body):
    """Pull the JSON block out of the review email body and parse it."""
    start = email_body.find(JSON_START_MARKER)
    end = email_body.find(JSON_END_MARKER)
    if start == -1 or end == -1:
        print("  [!] Could not find JSON markers in the weekly review email.")
        return None
    json_str = email_body[start + len(JSON_START_MARKER):end].strip()
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"  [!] Failed to parse suggestions JSON: {e}")
        return None


def parse_approved_indices(reply_text, total_count):
    """Use Claude Haiku to parse which suggestion numbers the user approved.
    Returns a sorted list of approved ints."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"There are {total_count} numbered suggestions (1 through {total_count}). "
                f"The user replied approving some of them. Parse their reply and return "
                f"ONLY a JSON array of approved integers.\n\n"
                f"Rules: 'all' means [1..{total_count}], 'all except 2' means all but 2, "
                f"'1, 3' means [1, 3]. Ignore any quoted email content (lines starting "
                f"with '>' or preceded by 'On ... wrote:').\n\n"
                f"User reply:\n{reply_text[:1500]}"
            ),
        }],
    )
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        indices = json.loads(raw)
        return sorted({i for i in indices if isinstance(i, int) and 1 <= i <= total_count})
    except json.JSONDecodeError:
        print(f"  [!] Could not parse approved indices: {raw[:200]}")
        return []


def _make_classifier_description(label_name, reason, examples):
    """Ask Haiku to turn the review's reason into a terse classifier description."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": (
                f"Write a one-sentence description of what emails belong in a Gmail label, "
                f"for use in an AI email classifier. Be specific and terse — similar in style "
                f"to: 'Job alerts or job listings, e.g. from LinkedIn, Indeed, job boards.'\n\n"
                f"Label: {label_name}\n"
                f"Why it was created: {reason}\n"
                f"Examples: {', '.join(str(e) for e in examples[:3]) if examples else 'none'}\n\n"
                f"Reply with ONLY the description sentence, no quotes around it."
            ),
        }],
    )
    return response.content[0].text.strip().strip('"')


def _update_labels_file(label_name, description):
    """Add a new label+description to labels.json."""
    path = os.path.join(SCRIPT_DIR, "labels.json")
    with open(path) as f:
        labels = json.load(f)
    if label_name in labels:
        print(f"    [SKIP] '{label_name}' already in labels.json")
        return
    labels[label_name] = description
    with open(path, "w") as f:
        json.dump(labels, f, indent=2)
        f.write("\n")
    print(f"    [OK] Added '{label_name}' to labels.json")


def apply_label(service, label_cache, suggestion):
    label_name = suggestion["label_name"]
    label_id = get_or_create_label_id(service, label_cache, label_name)
    ok = label_id is not None
    print(f"  [{'OK' if ok else 'FAILED'}] Create label: {label_name}")
    if ok:
        description = _make_classifier_description(
            label_name,
            suggestion.get("reason", ""),
            suggestion.get("matching_senders_or_subjects", []),
        )
        _update_labels_file(label_name, description)
    return ok


def apply_filter(service, label_cache, suggestion):
    label_name = suggestion["label_name"]
    query = suggestion["gmail_filter_query"]
    label_id = get_or_create_label_id(service, label_cache, label_name)
    if not label_id:
        print(f"  [SKIP] Filter {query} -> {label_name} (label resolution failed)")
        return False

    try:
        service.users().settings().filters().create(
            userId="me",
            body={
                "criteria": {"query": query},
                "action": {"addLabelIds": [label_id]},
            },
        ).execute()
        print(f"  [OK] Filter: {query} -> {label_name}")
        return True
    except HttpError as error:
        print(f"  [FAILED] Filter: {query} -> {label_name}: {error}")
        return False


def apply_retroactive_relabel(service, label_cache, suggestion):
    query = suggestion["query"]
    add_label = suggestion["add_label"]
    remove_labels = suggestion.get("remove_labels", [])

    matches = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        results = service.users().messages().list(**kwargs).execute()
        matches.extend(results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    if not matches:
        print(f"  [NONE FOUND] {query}")
        return False

    add_id = get_or_create_label_id(service, label_cache, add_label)
    remove_ids = [
        lid for lid in (
            get_or_create_label_id(service, label_cache, name)
            for name in remove_labels
        ) if lid
    ]

    body = {}
    if add_id:
        body["addLabelIds"] = [add_id]
    if remove_ids:
        body["removeLabelIds"] = remove_ids

    for msg in matches:
        service.users().messages().modify(userId="me", id=msg["id"], body=body).execute()

    print(f"  [OK] {len(matches)} email(s) -> +{add_label} -{remove_labels} ({query})")
    return True


def mark_as_applied(service, label_cache, message_id):
    applied_id = get_or_create_label_id(service, label_cache, APPLIED_LABEL)
    if applied_id:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [applied_id]},
        ).execute()


def send_confirmation(service, my_email, thread_id, review_message_id, applied, not_found):
    """Reply into the weekly review thread with a summary of what was applied."""
    orig = service.users().messages().get(
        userId="me",
        id=review_message_id,
        format="metadata",
        metadataHeaders=["Message-ID", "Subject"],
    ).execute()
    headers = orig["payload"]["headers"]
    orig_message_id = next((h["value"] for h in headers if h["name"].lower() == "message-id"), None)
    orig_subject = next(
        (h["value"] for h in headers if h["name"].lower() == "subject"),
        REVIEW_SUBJECT_PREFIX,
    )

    lines = [f"Inbox automation applied -- {datetime.now().strftime('%Y-%m-%d')}", ""]
    if applied:
        lines.append("Applied:")
        for desc in applied:
            lines.append(f"  - {desc}")
    if not_found:
        lines.append("\nNothing found to relabel (queries matched 0 emails):")
        for desc in not_found:
            lines.append(f"  - {desc}")
    if not applied and not not_found:
        lines.append("No changes were made (all approved suggestions may have already existed).")

    msg = MIMEText("\n".join(lines))
    msg["to"] = my_email
    msg["from"] = my_email
    msg["subject"] = f"Re: {orig_subject}"
    if orig_message_id:
        msg["In-Reply-To"] = orig_message_id
        msg["References"] = orig_message_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": thread_id},
    ).execute()
    print("Confirmation reply sent.")


def main():
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    review_id, thread_id, review_body = find_latest_unapplied_review(service)
    if not review_id:
        print("No unapplied weekly review found.")
        return

    profile = service.users().getProfile(userId="me").execute()
    my_email = profile["emailAddress"]

    reply_text = find_user_reply(service, thread_id, review_id, my_email)
    if not reply_text:
        print("No reply found yet -- will check again on the next scheduled run.")
        return

    data = extract_suggestions_json(review_body)
    if not data:
        return

    suggestions = data.get("suggestions", [])
    if not suggestions:
        print("No suggestions found in the JSON block.")
        return

    approved = parse_approved_indices(reply_text, len(suggestions))
    if not approved:
        print("Could not parse any approved suggestion numbers from the reply.")
        return

    print(f"Applying {len(approved)} approved suggestion(s): {approved}\n")

    label_cache = {}
    applied = []
    not_found = []

    for s in suggestions:
        if s["index"] not in approved:
            continue
        stype = s["type"]
        if stype == "label":
            if apply_label(service, label_cache, s):
                applied.append(f"New label: {s['label_name']}")
        elif stype == "filter":
            if apply_filter(service, label_cache, s):
                applied.append(f"Filter: {s['gmail_filter_query']} -> {s['label_name']}")
        elif stype == "retroactive":
            ok = apply_retroactive_relabel(service, label_cache, s)
            desc = f"Retroactive: {s['query']} -> {s['add_label']}"
            (applied if ok else not_found).append(desc)

    mark_as_applied(service, label_cache, review_id)

    print()
    send_confirmation(service, my_email, thread_id, review_id, applied, not_found)
    print("\nDone.")


if __name__ == "__main__":
    main()
