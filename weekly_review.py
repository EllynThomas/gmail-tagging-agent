"""
Weekly review: analyze the past week's labeled emails + everything in
Needs Review, ask Claude for label/sub-label suggestions and Gmail filter
ideas, and email the suggestions to yourself.

This script does NOT create any new labels on its own -- it only sends a
summary email. New labels get created later, on request, in a future
chat/run once you've reviewed and approved the suggestions.

Run this on its own weekly schedule (separate from classify_emails.py),
e.g. via cron every Monday at 3am.

Requires ANTHROPIC_API_KEY to be set as an environment variable.
Requires the same credentials.json / token.json as classify_emails.py,
but token.json must include the gmail.send scope (see classify_emails.py
SCOPES -- if you added gmail.modify before adding gmail.send, delete
token.json and re-authorize once interactively).
"""

import base64
import json
import os.path
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import anthropic

# Must match (or be a superset of) the scopes in classify_emails.py, since
# both scripts share the same token.json.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

# How many emails to pull in for review (most recent first). Keep this
# reasonably high since it's only running once a week.
MAX_RESULTS = 200

PROCESSED_LABEL = "HBF"
NEEDS_REVIEW_LABEL = "Needs Review"

# Your current label scheme, kept here too so the review prompt has full
# context on what already exists (avoids suggesting near-duplicates).
EXISTING_CUSTOM_LABELS = [
    "Job/Board", "Job/Replies", "Job/Sent Applications", "Job/Alerts",
    "Notes", "Payslips", "Receipts", "Receipts/Lime", "Receipts/Travel",
    "Tickets", "Uni", "PM/Fashion", "PM/Travel", "PM/Events", "PM/Other",
    "PM/Newsletters", "PM/Gaming", "Dracula", "Junk", "News", "Courses",
    "Security/Alerts", "Subscriptions", "Needs Review",
]

client = anthropic.Anthropic()

JSON_START_MARKER = "--- BEGIN SUGGESTIONS JSON (do not modify) ---"
JSON_END_MARKER = "--- END SUGGESTIONS JSON ---"


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


def fetch_emails_for_review(service):
    """Pull emails labeled in the last 30 days (HBF) plus anything
    currently sitting in Needs Review, regardless of age. Each email comes
    back with the human-readable names of its currently applied labels,
    so the review prompt can see exactly where things landed."""
    one_month_ago = datetime.now() - timedelta(days=30)
    date_cutoff = one_month_ago.strftime("%Y/%m/%d")

    # Build an id -> name lookup once, up front.
    label_list = service.users().labels().list(userId="me").execute()
    id_to_name = {l["id"]: l["name"] for l in label_list.get("labels", [])}

    # Recently processed emails (any outcome).
    recent_query = f"label:{PROCESSED_LABEL} after:{date_cutoff}"
    recent_results = (
        service.users()
        .messages()
        .list(userId="me", q=recent_query, maxResults=MAX_RESULTS)
        .execute()
    )
    recent_ids = {m["id"] for m in recent_results.get("messages", [])}

    # Everything currently in Needs Review, regardless of when it landed.
    review_query = f'label:"{NEEDS_REVIEW_LABEL}"'
    review_results = (
        service.users()
        .messages()
        .list(userId="me", q=review_query, maxResults=MAX_RESULTS)
        .execute()
    )
    review_ids = {m["id"] for m in review_results.get("messages", [])}

    all_ids = recent_ids | review_ids

    emails = []
    for msg_id in all_ids:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = msg["payload"]["headers"]
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(no subject)")
        sender = next((h["value"] for h in headers if h["name"] == "From"), "(unknown sender)")
        applied_label_ids = msg.get("labelIds", [])
        applied_label_names = [
            id_to_name.get(lid, lid) for lid in applied_label_ids
            if id_to_name.get(lid, lid) not in ("INBOX", "UNREAD", PROCESSED_LABEL)
            and not id_to_name.get(lid, lid).startswith("CATEGORY_")
        ] or ["(none)"]

        emails.append({
            "sender": sender,
            "subject": subject,
            "snippet": msg.get("snippet", ""),
            "current_labels": applied_label_names,
        })

    return emails, review_ids


def build_review_prompt(emails, needs_review_count):
    existing_labels_list = "\n".join(f"- {name}" for name in EXISTING_CUSTOM_LABELS)

    emails_block = "\n".join(
        f"- From: {e['sender']} | Subject: {e['subject']} | "
        f"Currently labeled: {', '.join(e['current_labels'])}"
        for e in emails
    )

    return f"""You are reviewing a month's worth of sorted emails for a Gmail
inbox-filtering system, to suggest improvements to the label scheme.

EXISTING CUSTOM LABELS:
{existing_labels_list}

This month's emails ({len(emails)} total, including {needs_review_count}
currently in "Needs Review"), with where each one currently landed:
{emails_block}

You have two separate jobs. Read both carefully -- they look for different
things.

JOB 1 -- FILTER SUGGESTIONS (high confidence, mechanical sender->label rules)
Look for senders where EVERY email from that exact sender/domain always
gets the same label, with no judgment call needed. These are good
candidates for a Gmail filter that bypasses the AI classifier entirely
(cheaper and faster than asking Claude every time).

Examples of the kind of pattern to look for:
- All "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>" emails get
  labeled Job/Board -> suggest filter: from:jobalerts-noreply@linkedin.com -> Job/Board
- All emails from a news outlet's domain (e.g. Telegraph, BBC) get
  labeled News -> suggest filter: from:telegraph.co.uk -> News
- All emails from a learning platform (e.g. edX, Coursera) get labeled
  Courses -> suggest filter: from:edx.org -> Courses

Only suggest a filter if you see the SAME sender/domain appear multiple
times with the SAME label every time. Don't suggest a filter for a sender
you've only seen once, or where the label varies.

JOB 2 -- NEW LABEL / SUB-LABEL SUGGESTIONS (finding sub-patterns)
Look in TWO places for these:
(a) Inside existing labels that are catching a mixed bag -- e.g. if
    several emails inside "PM/Other" are actually all tech product
    promotions, that's a sign "PM/Tech" deserves to be its own sub-label.
(b) Inside "Needs Review" and "Junk" -- if several emails there share an
    obvious unaddressed theme (e.g. multiple security/login alert emails
    from different services), that's a sign a new label like "Security
    Alerts" would help.

Only suggest a new label if you see at least 2-3 real examples of the
pattern in this batch -- not a single one-off email.

IMPORTANT: the "label_name" field must be the exact Gmail label name to
create -- short, clean, no parentheses, no notes (e.g. "PM/Politics" not
"PM/Politics (new)" or "PM/Politics -- see reason"). Put any explanation
in the "reason" field instead.

JOB 3 -- RETROACTIVE RELABELING (moving specific backlogged emails)
For emails sitting in "Needs Review", "Junk", or a catch-all label that
clearly belong under a better label (existing or newly suggested in JOB 2),
generate a precise Gmail search query to find those specific emails.

Use from: and/or subject: keywords narrow enough to target only the emails
you actually saw in this batch -- not broad sender-only queries that would
match future emails of a different type from the same sender.

Good example: from:automated@airbnb.com subject:"Security alert"
Bad example:  from:airbnb.com  (too broad -- matches receipts, marketing, etc.)

For each retroactive relabel, also list which labels should be removed
(e.g. "Needs Review", "PM/Other") alongside the label being added.

Only suggest retroactive relabels where you have concrete examples in this
batch. If you have no examples, return an empty array.

Respond with ONLY valid JSON in this exact format (empty arrays if you
genuinely have no suggestions for a job):
{{
  "filter_suggestions": [
    {{"label_name": "Job/Board", "gmail_filter_query": "from:jobalerts-noreply@linkedin.com", "reason": "Every LinkedIn job alert email this month was labeled Job/Board"}}
  ],
  "new_label_suggestions": [
    {{"label_name": "PM/Tech", "reason": "Several tech product promo emails are landing in PM/Other -- label_name must be short and clean, no parentheses or notes", "matching_senders_or_subjects": ["example sender/subject 1", "example sender/subject 2"]}}
  ],
  "retroactive_relabels": [
    {{"query": "from:automated@airbnb.com subject:Security alert", "add_label": "Security/Alerts", "remove_labels": ["Needs Review"], "reason": "3 Airbnb security alert emails are sitting in Needs Review"}}
  ]
}}"""


def get_review_suggestions(emails, needs_review_count):
    if not emails:
        return {"new_label_suggestions": [], "filter_suggestions": [], "retroactive_relabels": []}

    prompt = build_review_prompt(emails, needs_review_count)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text.strip()
    raw_text = raw_text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"  [!] Could not parse review suggestions: {raw_text[:500]}")
        return {"new_label_suggestions": [], "filter_suggestions": [], "retroactive_relabels": []}


def assign_indices(raw_suggestions):
    """Flatten the three suggestion types into a single sequentially-indexed list."""
    indexed = []
    idx = 1
    for s in raw_suggestions.get("new_label_suggestions", []):
        indexed.append({"index": idx, "type": "label", **s})
        idx += 1
    for s in raw_suggestions.get("filter_suggestions", []):
        indexed.append({"index": idx, "type": "filter", **s})
        idx += 1
    for s in raw_suggestions.get("retroactive_relabels", []):
        indexed.append({"index": idx, "type": "retroactive", **s})
        idx += 1
    return indexed


def format_summary_email(suggestions, total_emails, needs_review_count):
    indexed = assign_indices(suggestions)

    label_items = [s for s in indexed if s["type"] == "label"]
    filter_items = [s for s in indexed if s["type"] == "filter"]
    retro_items = [s for s in indexed if s["type"] == "retroactive"]

    lines = [
        f"Weekly inbox review -- {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"Reviewed {total_emails} emails from the past month "
        f"({needs_review_count} currently in Needs Review).",
        "",
    ]

    if label_items:
        lines.append("--- Suggested new labels/sub-labels ---")
        for s in label_items:
            lines.append(f"\n[{s['index']}] {s.get('label_name', '(unnamed)')}")
            lines.append(f"    Reason: {s.get('reason', '')}")
            examples = s.get("matching_senders_or_subjects", [])
            if examples:
                lines.append(f"    Examples: {', '.join(examples)}")
    else:
        lines.append("No new label suggestions this week.")

    lines.append("")

    if filter_items:
        lines.append("--- Suggested Gmail filters ---")
        for s in filter_items:
            lines.append(
                f"\n[{s['index']}] {s.get('gmail_filter_query', '')} -> {s.get('label_name', '')}"
            )
            lines.append(f"    Reason: {s.get('reason', '')}")
    else:
        lines.append("No filter suggestions this week.")

    lines.append("")

    if retro_items:
        lines.append("--- Suggested retroactive relabels ---")
        for s in retro_items:
            remove = s.get("remove_labels", [])
            remove_str = f" (remove: {', '.join(remove)})" if remove else ""
            lines.append(
                f"\n[{s['index']}] {s.get('query', '')} -> {s.get('add_label', '')}{remove_str}"
            )
            lines.append(f"    Reason: {s.get('reason', '')}")
    else:
        lines.append("No retroactive relabel suggestions this week.")

    lines.append("")

    if indexed:
        lines.append(
            'Reply to this email with the suggestion numbers you want applied '
            '(e.g. "1, 3" or "all" or "all except 2"). '
            'apply_suggestions.py will pick it up on Tuesday or Friday.'
        )
    else:
        lines.append("No suggestions this week -- nothing to approve.")

    lines.append("")
    lines.append(JSON_START_MARKER)
    lines.append(json.dumps({"suggestions": indexed}))
    lines.append(JSON_END_MARKER)

    return "\n".join(lines)


def send_summary_email(service, body_text):
    profile = service.users().getProfile(userId="me").execute()
    my_email = profile["emailAddress"]

    message = MIMEText(body_text)
    message["to"] = my_email
    message["from"] = my_email
    message["subject"] = f"Weekly inbox review -- {datetime.now().strftime('%Y-%m-%d')}"

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


def main():
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    print("Fetching this month's emails for review...")
    emails, review_ids = fetch_emails_for_review(service)

    if not emails:
        print("No emails to review this month.")
        return

    print(f"Reviewing {len(emails)} emails ({len(review_ids)} in Needs Review)...")
    suggestions = get_review_suggestions(emails, len(review_ids))

    summary = format_summary_email(suggestions, len(emails), len(review_ids))
    print("\n" + summary + "\n")

    send_summary_email(service, summary)
    print("Summary email sent.")


if __name__ == "__main__":
    main()
