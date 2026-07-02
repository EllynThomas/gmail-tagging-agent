# gmail-tagging-agent

Automatically classifies Gmail inbox emails into labels using Claude AI, with a weekly review workflow for refining your label scheme over time.

## How it works

- **`classify_emails.py`** — runs on a schedule, fetches recent unlabelled inbox emails, sends them to Claude Haiku in a single batch call, and applies Gmail labels + category tabs.
- **`weekly_review.py`** — runs weekly, reviews the past month of labelled emails, and emails you a numbered list of suggestions (new labels, Gmail filters, retroactive relabels).
- **`apply_suggestions.py`** — checks for your reply to the weekly review email. Reply with the suggestion numbers you want applied (`1, 3`, `all`, `all except 2`) and this script applies them automatically.
- **`test_suite.py`** — verifies your setup end-to-end (credentials, API access, label file).

---

## Setup

### 1. Clone the repo and create a virtual environment

```bash
git clone git@github.com:EllynThomas/gmail-tagging-agent.git
cd gmail-tagging-agent
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client anthropic
```

### 2. Set up the Gmail API

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. In the left menu go to **APIs & Services → Library**, search for **Gmail API**, and enable it.
3. Go to **APIs & Services → OAuth consent screen**:
   - Choose **External**, fill in an app name, add your Gmail address as a test user.
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Download the JSON file and save it as **`credentials.json`** in the project directory.

### 3. Set up the Anthropic API

1. Create an account at [console.anthropic.com](https://console.anthropic.com/) and generate an API key.
2. Set it as an environment variable:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

For a persistent setup (server/cron), add it to your crontab or shell profile.

### 4. Authorise Gmail access

Run any script for the first time to trigger the OAuth browser flow:

```bash
python classify_emails.py
```

A browser window will open asking you to sign in and grant access. This creates `token.json` — keep it safe and never commit it.

### 5. Verify everything works

```bash
python test_suite.py
```

All checks should pass before running the main scripts.

---

## Configuration

The main tuneable values are at the top of each script — no need to dig into the logic to change them.

**`classify_emails.py`**

| Constant | Default | What it controls |
|---|---|---|
| `MAX_RESULTS` | `200` | Max emails classified per run. Increase if you get a lot of mail; decrease to reduce API cost. |
| `timedelta(days=2)` | `2` | How far back to look for unlabelled emails. Increase if you run the script less frequently than daily. |

**`weekly_review.py`**

| Constant | Default | What it controls |
|---|---|---|
| `MAX_RESULTS` | `200` | Max emails pulled in for the weekly review prompt. |
| `timedelta(days=30)` | `30` | How far back the review looks when fetching labelled emails for analysis. |

**`apply_suggestions.py`**

| Constant | Default | What it controls |
|---|---|---|
| `REVIEW_SUBJECT_PREFIX` | `"Weekly inbox review --"` | The subject prefix used to find your review emails. Change if you edit `weekly_review.py`'s subject line. |
| `APPLIED_LABEL` | `"Review/Applied"` | Gmail label added to review emails once suggestions are applied, to prevent double-processing. |

---

## Customising your labels

The label scheme lives in `labels.default.json` (committed, generic defaults). To personalise:

1. Copy it to `labels.json`:
   ```bash
   cp labels.default.json labels.json
   ```
2. Edit `labels.json` — add, remove, or tweak labels and their classifier descriptions.

`labels.json` is gitignored so your personal version stays local. The scripts always prefer `labels.json` if it exists, and fall back to `labels.default.json` otherwise.

### Label format

```json
{
  "Label name": "One-sentence description used by the AI classifier to decide what belongs here.",
  "Parent/Child": "Sub-labels use a slash. The parent label is applied automatically alongside the sub-label."
}
```

Keep label names short and clean — no parentheses or notes in the name itself. Put explanations in the description.

### Adding labels via the weekly review

You don't have to edit the file manually. When `weekly_review.py` suggests a new label and you approve it, `apply_suggestions.py` creates the Gmail label, generates a classifier description automatically, and adds it to `labels.json`.

---

## Running on a schedule (cron)

Example crontab (`crontab -e`) for running on a Linux server:

```
# Classify new inbox emails every day at 6am
0 6 * * * cd /path/to/gmail-tagging-agent && /path/to/.venv/bin/python classify_emails.py >> classify.log 2>&1

# Send weekly review email every Monday at 7am
0 7 * * 1 cd /path/to/gmail-tagging-agent && /path/to/.venv/bin/python weekly_review.py >> review.log 2>&1

# Check for approved suggestions on Tuesday and Friday at 8am
0 8 * * 2,5 cd /path/to/gmail-tagging-agent && /path/to/.venv/bin/python apply_suggestions.py >> apply.log 2>&1
```

Set `ANTHROPIC_API_KEY` in the crontab environment:

```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Weekly review workflow

1. Every Monday, `weekly_review.py` emails you a numbered list of suggestions.
2. Reply to the email with the numbers you want applied — e.g. `1, 3` or `all` or `all except 2`.
3. On Tuesday (or Friday if you haven't replied yet), `apply_suggestions.py` picks up your reply and applies the approved suggestions, then sends a confirmation reply.

---

## Deploying to AWS EC2

Running this on a server means the scripts execute on a schedule without your laptop being on.

### 1. Launch an EC2 instance

1. Go to [EC2 Console](https://console.aws.amazon.com/ec2/) → **Launch instance**.
2. Choose **Ubuntu Server 24.04 LTS** (64-bit x86).
3. Instance type: **t2.micro** (free tier, more than enough).
4. **Key pair**: create a new one or use an existing one — download the `.pem` file and keep it safe.
5. **Security group**: allow **SSH (port 22)** from your IP only. No other inbound ports needed.
6. Launch the instance and note its **Public IPv4 address**.

### 2. Connect and set up the server

```bash
# Fix key permissions (required by SSH)
chmod 400 your-key.pem

# Connect
ssh -i your-key.pem ubuntu@<your-server-ip>
```

Once connected, install Python and clone the repo:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/EllynThomas/gmail-tagging-agent.git
cd gmail-tagging-agent
python3 -m venv .venv
source .venv/bin/activate
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client anthropic
```

### 3. Upload your credentials

The OAuth browser flow can't run on a headless server, so complete it locally first (step 4 of Setup above) to generate `token.json`, then upload both files:

```bash
# Run this on your local machine, not the server
scp -i your-key.pem credentials.json token.json ubuntu@<your-server-ip>:~/gmail-tagging-agent/
```

If you have a personal `labels.json`, upload that too:

```bash
scp -i your-key.pem labels.json ubuntu@<your-server-ip>:~/gmail-tagging-agent/
```

### 4. Set up cron on the server

SSH back into the server and open the crontab:

```bash
crontab -e
```

Add the following (adjust the path if you cloned to a different location):

```
ANTHROPIC_API_KEY=sk-ant-...

# Classify new inbox emails every day at 6am
0 6 * * * cd /home/ubuntu/gmail-tagging-agent && /home/ubuntu/gmail-tagging-agent/.venv/bin/python classify_emails.py >> run.log 2>&1

# Send weekly review email every Monday at 7am
0 7 * * 1 cd /home/ubuntu/gmail-tagging-agent && /home/ubuntu/gmail-tagging-agent/.venv/bin/python weekly_review.py >> run.log 2>&1

# Check for approved suggestions on Tuesday and Friday at 8am
0 8 * * 2,5 cd /home/ubuntu/gmail-tagging-agent && /home/ubuntu/gmail-tagging-agent/.venv/bin/python apply_suggestions.py >> run.log 2>&1
```

### 5. Verify

```bash
cd ~/gmail-tagging-agent
source .venv/bin/activate
python test_suite.py
```

### Deploying future updates

Once set up, pulling updates from GitHub is a one-liner:

```bash
ssh -i your-key.pem ubuntu@<your-server-ip> "cd ~/gmail-tagging-agent && git pull"
```

---

## Files

| File | Committed | Notes |
|---|---|---|
| `credentials.json` | No | Download from Google Cloud Console |
| `token.json` | No | Created on first run |
| `labels.json` | No | Your personal label scheme |
| `labels.default.json` | Yes | Generic starting point |
| `classify_emails.py` | Yes | Daily classifier |
| `weekly_review.py` | Yes | Weekly suggestion emailer |
| `apply_suggestions.py` | Yes | Applies approved suggestions |
| `test_suite.py` | Yes | Setup verification |
