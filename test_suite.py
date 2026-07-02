"""
Setup verification for gmail-tagging-agent.

Run this after cloning the repo or deploying to a new server to confirm
that credentials, API keys, and labels are all working correctly.

Usage:
  python test_suite.py
"""

import json
import os
import sys


def check(label, fn):
    print(f"  {label}...", end=" ", flush=True)
    try:
        result = fn()
        print(f"OK{f' ({result})' if result else ''}")
        return True
    except Exception as e:
        print(f"FAILED\n    {e}")
        return False


def main():
    passed = 0
    failed = 0

    print("\n=== gmail-tagging-agent setup check ===\n")

    # --- labels file ---
    labels_file = "labels.json" if os.path.exists("labels.json") else "labels.default.json"
    print(f"[ {labels_file} ]")

    def load_labels():
        with open(labels_file) as f:
            labels = json.load(f)
        assert isinstance(labels, dict) and len(labels) > 0
        return f"{len(labels)} labels"

    def check_label_names():
        with open(labels_file) as f:
            labels = json.load(f)
        bad = [n for n in labels if "(" in n]
        assert not bad, f"Labels with parentheses in name: {bad}"

    for label, fn in [
        ("File exists and parses", load_labels),
        ("Label names are clean", check_label_names),
    ]:
        if check(label, fn):
            passed += 1
        else:
            failed += 1

    # --- Gmail credentials ---
    print("\n[ Gmail credentials ]")

    def gmail_files_exist():
        assert os.path.exists("credentials.json"), "credentials.json not found"
        assert os.path.exists("token.json"), "token.json not found"

    def gmail_token_scopes():
        with open("token.json") as f:
            token = json.load(f)
        scopes = token.get("scopes", [])
        required = {
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.settings.basic",
        }
        missing = required - set(scopes)
        assert not missing, f"Missing scopes: {missing}"
        return f"{len(scopes)} scopes"

    def gmail_connect():
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file("token.json")
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        return profile["emailAddress"]

    def gmail_labels_exist():
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file("token.json")
        service = build("gmail", "v1", credentials=creds)
        result = service.users().labels().list(userId="me").execute()
        gmail_names = {l["name"].lower() for l in result["labels"]}
        with open(labels_file) as f:
            local_labels = json.load(f)
        missing = [n for n in local_labels if n.lower() not in gmail_names]
        if missing:
            print(f"\n    Note: these labels aren't in Gmail yet (will be created on first classify run): {missing}", end="")

    for label, fn in [
        ("credentials.json and token.json present", gmail_files_exist),
        ("token.json has required scopes", gmail_token_scopes),
        ("Gmail API connection", gmail_connect),
        ("Labels in Gmail match labels.json", gmail_labels_exist),
    ]:
        if check(label, fn):
            passed += 1
        else:
            failed += 1

    # --- Anthropic API ---
    print("\n[ Anthropic API ]")

    def anthropic_key_set():
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        assert key, "ANTHROPIC_API_KEY environment variable is not set"
        assert key != "unit-test-placeholder", "ANTHROPIC_API_KEY is still set to the placeholder value"
        return f"key ending ...{key[-4:]}"

    def anthropic_classify():
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": 'Reply with exactly: ["Job/Board"]'}],
        )
        text = response.content[0].text.strip()
        assert "Job/Board" in text, f"Unexpected response: {text}"
        return "Haiku responded correctly"

    for label, fn in [
        ("ANTHROPIC_API_KEY is set", anthropic_key_set),
        ("Anthropic API call works", anthropic_classify),
    ]:
        if check(label, fn):
            passed += 1
        else:
            failed += 1

    # --- Summary ---
    print(f"\n{'='*38}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*38}\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
