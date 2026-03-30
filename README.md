# Sales AI Agents

A suite of AI-powered sales automation agents that identify opportunities and generate personalized outreach emails by researching prospects across multiple data sources.

> ⚠️ **Note**: These agents require customization before use. The AI prompts are currently configured for a specific product. See the [Customize AI Prompts](#customize-ai-prompts-required) section to adapt them for your product/service.

## Agents

### 1. Follow-up Agent (`followup_agent.py`)
Identifies **stale deals** in your HubSpot pipeline and generates follow-up emails.

### 2. Lead Finder Agent (`lead_finder_agent.py`)
Finds **high-engagement Marketing Contacts** without deals, generates outreach emails to re-engage them (using **previous email thread context** from HubSpot for better personalization), and generates **LinkedIn connection notes** (max 300 characters) for use when sending connection requests. Lead count is configurable via `TOP_LEADS_COUNT` and split between assigned recipients.

### 3. CSV Outreach Email Generator (`csv_outreach_emails.py`)
Reads a **CSV of contacts** (for example from events or list uploads), automatically detects column headers, optionally enriches contacts via **Apollo**, pulls context from the shared **ChromaDB knowledge base**, and can use **Parallel.ai web search** for extra context. Generates **short, personalized LinkedIn InMail-style messages** (around 80 words) for each row and outputs either JSON or a new CSV with an `email_body` column.

### 4. Executive Summary Agent (`executive_summary_agent.py`)
Runs daily (e.g. every evening) to build an **executive deal pipeline summary**. Fetches all active deals in configured stages (e.g. Potential Fit, Proposal Sent, Negotiation, Contract), gathers context per deal (contact, company, last email, last note, days since activity), and uses Claude to generate a **4–6 sentence executive summary** per deal. Sends a single HTML digest grouped by stage to `EXECUTIVE_SUMMARY_RECIPIENTS`. No stale filter — every deal in those stages is included.

## Features

### Follow-up Agent
- **CRM Integration**: Queries HubSpot for deals in specific pipeline stages
- **Multi-Source Research**: Gathers context from:
  - 📧 HubSpot emails and notes from Contact, Deal & Account objects (fetched and included in context; not searched separately)
  - 💬 Slack internal discussions
  - 📞 Fireflies call transcripts (searched by company name and company domain from website)
  - 🌐 Web search for company news & AI initiatives
  - 📚 Knowledge base (RAG) for relevant product context
- **AI-Powered Emails**: Uses Claude to generate personalized, context-rich follow-up emails
- **Daily Digest**: Sends a beautifully formatted HTML digest with all draft emails ready to review

### Lead Finder Agent
- **Smart Filtering**: Finds Marketing Contacts with high engagement but no recent activity
- **Configurable Lead Count**: `TOP_LEADS_COUNT` sets total leads per run; leads are split evenly between assigned recipients (e.g. 10 leads → 5 per person)
- **Configurable Filters**: Filter by employee size, industry, country, job title, lifecycle stage
- **Multi-Source Enrichment**:
  - 📧 **HubSpot previous emails** – Fetches up to 15 prior emails with the contact so the AI can reference the thread, avoid repetition, and build on past conversations
  - 🔍 Apollo.io for company funding, tech stack, and hiring signals
  - 💬 Slack internal discussions
  - 📞 Fireflies call transcripts (searched by company name and company domain from website/Apollo)
  - 📝 HubSpot contact notes (fetched and included in context; not searched separately)
  - 📚 Knowledge base (RAG) for relevant product context
- **Engagement Scoring**: Ranks leads by HubSpot lead scoring or custom engagement (email opens, clicks, page views, form submissions)
- **AI-Powered Outreach**: Generates personalized re-engagement emails using Claude (with previous-email context for follow-ups)
- **LinkedIn Connection Notes**: Generates a short, personalized note (max 300 characters) for each lead to use when sending a LinkedIn connection request; included in the digest

### CSV Outreach Email Generator
- **CSV-First Workflow**: Bring your own CSV (e.g. conference scans, exported lists). The script automatically maps common headers like company, job title, full name, email, sector, website, LinkedIn, location, and interest flags.
- **Header-Aware Prompting**: On each run, it calls Claude once to generate a **CSV-specific context block** based on your actual headers so the model knows how to best use the columns you provided.
- **Optional Enrichment**: Uses Apollo to enrich contacts and companies (title, seniority, tech stack, funding, hiring signals) when `APOLLO_API_KEY` is set.
- **Shared Knowledge Base**: Reuses the same ChromaDB knowledge base as the other agents (via `index_knowledge_base.py`) to pull relevant capabilities, use cases, and persona messaging.
- **Optional Web Search**: When `PARALLEL_API_KEY` is set, calls the Parallel.ai Search API to add recent company or industry context.
- **Compact InMail Messages**: Generates a single **≈80-word LinkedIn InMail body** per contact, designed for quick follow-up after events like Manifest – Supply Chain Conference.

### Executive Summary Agent
- **Pipeline-Wide View**: Fetches all deals in configured stages (Potential Fit, Proposal Sent, Negotiation, Contract) with no stale filter
- **Context per Deal**: Deal name, stage, value, owner; contact (name, title, email); company (name, industry, size); last email date and subject; last note; days since last activity
- **AI Summaries**: Claude (claude-3-5-sonnet) generates a 4–6 sentence paragraph per deal: who they are, stage/value, last interaction, health (healthy/progressing/stuck), and one recommended next action
- **HTML Digest**: Single email grouped by stage with professional styling; stage badges (e.g. Potential Fit = yellow, Contract = green); each deal shows an AI summary box
- **Scheduling**: Intended to run daily (e.g. 6:00 PM IST); cron or GitHub Actions

## How It Works

### Follow-up Agent Flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   HubSpot CRM   │────▶│  Gather Context  │────▶│  Claude AI      │
│   (Stale Deals) │     │  from all sources│     │  (Generate      │
└─────────────────┘     └──────────────────┘     │   Emails)       │
                               ▲                 └────────┬────────┘
                               │                          │
                        ┌──────┴───────┐                  │
                        │ Knowledge    │                  │
                        │ Base (RAG)   │                  │
                        └──────────────┘                  │
                       ┌──────────────────┐               │
                       │  Email Digest    │◀──────────────┘
                       │  (SendGrid/SMTP) │
                       └──────────────────┘
```

1. **Query HubSpot** for deals in target stages (First Meeting, Demo, Potential Fit, Cold)
2. **Filter stale deals** where the last email was sent >14 days ago
3. **Research each deal** using Slack, Fireflies, web search, and knowledge base
4. **Generate personalized follow-ups** using Claude with full context
5. **Send a digest email** with all draft emails ready for review

### Lead Finder Agent Flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  HubSpot CRM    │────▶│  Apply Filters   │────▶│  Score & Rank   │
│  (Marketing     │     │  (size, industry │     │  by Engagement  │
│   Contacts)     │     │   title, etc.)   │     │                 │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
┌─────────────────┐     ┌──────────────────┐              │
│  Knowledge Base │────▶│  Enrich Context  │◀─────────────┘
│  (ChromaDB RAG) │     │  HubSpot emails, │
└─────────────────┘     │  Apollo, Slack,  │
                         │  Fireflies       │
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
                        │  Claude AI       │
                        │  (Email +        │
                        │   LinkedIn note)  │
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
                        │  Email Digest    │
                        │  (SendGrid)      │
                        └──────────────────┘
```

1. **Query HubSpot** for Marketing Contacts (optional: employee size, stale threshold)
2. **Filter contacts** without deals, matching your criteria; skip already-processed contacts
3. **Score engagement** (HubSpot lead scoring or email opens, clicks, page views, form submissions)
4. **Select top N** leads (N = `TOP_LEADS_COUNT`), split evenly between assigned recipients
5. **Enrich context** from HubSpot (previous emails with the contact), Apollo, Slack, Fireflies, and Knowledge Base
6. **Generate outreach emails** using Claude with full context (including prior email thread for follow-ups)
7. **Generate LinkedIn connection notes** (max 300 chars each) for use when sending connection requests
8. **Send a digest email** with draft emails and LinkedIn notes ready for review

### CSV Outreach Email Generator Flow

```
┌─────────────────┐      ┌──────────────────────┐      ┌─────────────────┐
│   CSV File      │ ───▶ │ Detect & Map Headers │ ───▶ │  Build Per-Lead │
│ (Contacts list) │      │  (email, company,    │      │  Context Block  │
└─────────────────┘      │  title, sector, etc.)│      └────────┬────────┘
                               │                                  │
                               ▼                                  │
                       ┌──────────────────┐                       │
                       │  Optional        │                       │
                       │  Apollo Enrich   │◀──────────────────────┘
                       │  + Knowledge Base│
                       │  + Web Search    │
                       └────────┬─────────┘
                                │
                        ┌───────▼────────┐
                        │   Claude AI    │
                        │  (InMail body) │
                        └────────┬───────┘
                                 │
                        ┌────────▼─────────┐
                        │   Output File    │
                        │ (JSON or CSV)    │
                        └──────────────────┘
```

1. **Read the CSV** and **auto-detect headers**, normalizing many common names (e.g. "E-mail Address", "Full name", "Job Title", "Priority Project 1").
2. **Build a CSV-specific context block** by calling Claude once with the header list so the system prompt understands what data this upload contains.
3. For each contact, **construct a context bundle** from CSV fields, optional Apollo enrichment, knowledge-base snippets, and optional Parallel.ai web search.
4. **Generate a single LinkedIn InMail-style body** (≈80 words) for that contact using the shared system prompt plus per-contact context.
5. **Write results** either as JSON (with full context and bodies) or as a new CSV with `email_id,email_body` columns suitable for uploading back into your tools.

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/sneurgaonkar/sales-ai-agents.git
cd sales-ai-agents
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 4. Run the Agents

```bash
# Run the Follow-up Agent (for stale deals)
python followup_agent.py

# Run the Lead Finder Agent (for high-engagement contacts)
python lead_finder_agent.py

# Run the Executive Summary Agent (evening pipeline digest)
python executive_summary_agent.py

# Run the CSV Outreach Email Generator (for CSV contact lists)
python csv_outreach_emails.py path/to/contacts.csv
# Optional flags:
#   --output emails.json        # JSON output (default if not .csv)
#   --output emails.csv         # CSV with email_id,email_body columns
#   --limit 5                   # process only first 5 rows
#   --no-apollo                 # skip Apollo enrichment
#   --no-kb                     # skip knowledge base search
#   --no-csv-context            # don't generate header-aware CSV context
```

### 5. Test with a Single Deal (Optional)

```bash
# Follow-up agent: test one deal
python test_single_deal.py "Deal Name"

# Executive Summary agent: test one deal (generates summary only, no email)
python test_executive_summary.py "Deal Name"
```

### 6. Set Up Knowledge Base (Optional)

Both the Follow-up Agent and Lead Finder Agent can use a knowledge base (RAG) to include relevant product context in emails:

```bash
# Add your documents to the docs/ folder
# Supported formats: .md, .txt, .pdf

# Index the knowledge base (first time)
python index_knowledge_base.py

# Re-index after adding new documents (incremental)
python index_knowledge_base.py

# Full re-index (rebuild from scratch)
python index_knowledge_base.py --full
```

## Requirements

### Python Dependencies

```
anthropic>=0.39.0
requests>=2.31.0
python-dotenv>=1.0.0
chromadb>=0.4.0           # For knowledge base (Lead Finder)
sentence-transformers>=2.2.0  # For embeddings
pypdf>=3.0.0              # For PDF document loading
```

### API Keys & Tokens

| Service | Follow-up Agent | Lead Finder Agent | Executive Summary | CSV Outreach Emails | Purpose |
|---------|-----------------|-------------------|-------------------|----------------------|---------|
| [Anthropic](https://console.anthropic.com/) | ✅ Required | ✅ Required | ✅ Required | ✅ Required | Claude AI for email / InMail / summary generation |
| [HubSpot](https://developers.hubspot.com/) | ✅ Required | ✅ Required | ✅ Required | ❌ Not used | CRM data (deals, contacts, companies) |
| [SendGrid](https://sendgrid.com/) | ✅ Required | ✅ Required | ✅ Required | ❌ Not used | Email delivery |
| [Slack](https://api.slack.com/) | ❌ Optional | ❌ Optional | ❌ Not used | ❌ Not used | Internal discussion search |
| [Fireflies.ai](https://fireflies.ai/) | ❌ Optional | ❌ Optional | ❌ Not used | ❌ Not used | Call transcript search |
| [Apollo.io](https://www.apollo.io/) | ❌ Not used | ❌ Optional | ❌ Not used | ❌ Optional | Contact/company enrichment |
| [Parallel.ai](https://parallel.ai/) | ❌ Not used | ❌ Not used | ❌ Not used | ❌ Optional | Web search enrichment for companies |

## Configuration

### Required Environment Variables

```bash
# API Keys
ANTHROPIC_API_KEY=sk-ant-xxxxx
HUBSPOT_ACCESS_TOKEN=pat-xxxxx

# Digest Settings
DIGEST_RECIPIENTS=user1@example.com,user2@example.com
FROM_EMAIL=noreply@example.com

# HubSpot Deal Stages to Monitor (REQUIRED)
# Find stage IDs: HubSpot Settings → Objects → Deals → Pipelines → click stage
TARGET_STAGES=appointmentscheduled,qualifiedtobuy,12345678
```

### Email Delivery (Choose One)

**Option A: SendGrid (Recommended)**
```bash
SENDGRID_API_KEY=SG.xxxxx
```

**Option B: SMTP**
```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your-app-password
```

### Optional Integrations

```bash
# Slack - for searching internal discussions
SLACK_BOT_TOKEN=xoxb-xxxxx
SLACK_CHANNELS=sales,marketing,support

# Fireflies - for searching call transcripts
FIREFLIES_API_KEY=xxxxx

# Parallel.ai - for CSV outreach web search enrichment (csv_outreach_emails.py)
PARALLEL_API_KEY=xxxxx

# Apollo.io - for CSV outreach enrichment (shared with Lead Finder)
APOLLO_API_KEY=xxxxx
```

### Optional Customization

```bash
# Days since last email to consider a deal stale
STALE_THRESHOLD_DAYS=14

# Default deal name for test script
TEST_DEAL_NAME=Test Deal
```

### Lead Finder Agent Configuration

```bash
# Digest recipients for lead finder
LEAD_FINDER_RECIPIENTS=user1@example.com,user2@example.com

# Apollo.io API key (optional, for enrichment)
APOLLO_API_KEY=xxxxx

# Contact filters (all optional)
MIN_EMPLOYEE_SIZE=200
TARGET_INDUSTRIES=Technology,Financial Services,Healthcare
TARGET_COUNTRIES=United States,Canada
TARGET_JOB_TITLES=VP,Director,Head of,Chief,Manager
TARGET_LIFECYCLE_STAGES=lead,marketingqualifiedlead

# Number of top leads per run (split evenly between recipients; e.g. 10 → 5 each)
TOP_LEADS_COUNT=10
```

### Executive Summary Agent Configuration

```bash
# Digest recipients for the evening pipeline summary
EXECUTIVE_SUMMARY_RECIPIENTS=email1@company.com,email2@company.com

# HubSpot deal stage IDs (same order as SUMMARY_STAGE_LABELS)
# Find IDs: HubSpot Settings → Objects → Deals → Pipelines
SUMMARY_STAGES=stage_id_1,stage_id_2,stage_id_3,stage_id_4

# Display names for each stage (same order as SUMMARY_STAGES)
SUMMARY_STAGE_LABELS=Potential Fit,Proposal Sent,Negotiation,Contract

# Email subject; {date} is replaced with today's date (e.g. 2025-03-17)
EXECUTIVE_SUMMARY_SUBJECT=Daily Deal Pipeline Summary — {date}

# HubSpot account ID (numeric) for "Open deal in HubSpot" links in the digest
# Find it in the URL when logged in: app.hubspot.com/contacts/{PORTAL_ID}/...
HUBSPOT_PORTAL_ID=12345678
```

Recommended: run daily at 6:00 PM IST (cron: `30 12 * * *` in UTC).

## Integration Setup

### HubSpot Private App

1. Go to **Settings → Integrations → Private Apps**
2. Create a new app with these scopes:
   - `crm.objects.deals.read`
   - `crm.objects.contacts.read`
   - `crm.objects.companies.read`
   - `sales-email-read` (required for Lead Finder to fetch previous emails with contacts)
3. Copy the access token to your `.env`

### Slack Bot (Optional)

1. Create a Slack App at [api.slack.com/apps](https://api.slack.com/apps)
2. Add OAuth scope: `search:read`
3. Install to your workspace
4. Add the bot to channels you want to search (e.g., `#sales`, `#marketing`)
5. Copy the Bot User OAuth Token to your `.env`

### Fireflies (Optional)

1. Go to [Fireflies Integrations](https://app.fireflies.ai/integrations)
2. Generate an API key
3. Copy to your `.env`

### Anthropic Web Search (Optional)

To enable web search for company news:
1. Go to [Anthropic Console](https://console.anthropic.com/settings/organization/features)
2. Enable the "Web Search" feature for your organization

### Apollo.io (Optional - Lead Finder Only)

1. Sign up at [Apollo.io](https://www.apollo.io/)
2. Go to Settings → API Keys
3. Generate an API key
4. Add to your `.env` as `APOLLO_API_KEY`

## Knowledge Base Setup

Both the Follow-up Agent and Lead Finder Agent use a vector database (ChromaDB) to retrieve relevant product information when generating emails. This helps create more personalized outreach based on the deal/lead's industry, persona, and context.

### 1. Add Documents

Place your documents in the `docs/` folder:

```
docs/
├── capabilities/
│   ├── feature-1.md
│   └── feature-2.md
├── use-cases/
│   ├── industry-1.md
│   └── industry-2.md
├── personas/
│   ├── cto-messaging.md
│   └── vp-engineering.md
└── case-studies/
    ├── customer-1.pdf
    └── customer-2.pdf
```

**Supported formats:** `.md`, `.txt`, `.pdf`

### 2. Index the Knowledge Base

```bash
# First time or after adding new documents
python index_knowledge_base.py

# Full re-index (if needed)
python index_knowledge_base.py --full
```

The indexer:
- Loads all documents from `docs/`
- Splits them into chunks (~1000 characters)
- Generates embeddings using `all-MiniLM-L6-v2` model
- Stores in ChromaDB (persisted in `.chroma/` folder)
- **Incremental**: Only processes new/modified files on subsequent runs

### 3. How RAG Works

When generating emails, both agents search the knowledge base for:
- **Industry-specific** use cases and capabilities
- **Persona-relevant** messaging (based on job title)
- **Context from notes** (problems, blockers, or specific needs mentioned)
- **Tech stack** integrations (from Apollo data, Lead Finder only)
- **General** product capabilities

This context is included in the Claude prompt to generate more relevant, personalized outreach.

| Agent | What RAG Searches For |
|-------|----------------------|
| Follow-up Agent | Industry, job title/persona, deal notes, general capabilities |
| Lead Finder Agent | Industry, job title/persona, tech stack (Apollo), general capabilities |

## Scheduling

### Option 1: Cron (Linux/Mac)

```bash
# Edit crontab
crontab -e

# Run at 9 AM daily
0 9 * * * cd /path/to/sales-followup-agent && python3 followup_agent.py >> /var/log/followup-agent.log 2>&1
```

### Option 2: GitHub Actions

Create `.github/workflows/followup.yml`:

```yaml
name: Daily Follow-up Agent

on:
  schedule:
    - cron: '0 16 * * *'  # 9 AM PST = 4 PM UTC
  workflow_dispatch:  # Manual trigger

jobs:
  run-agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python followup_agent.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          HUBSPOT_ACCESS_TOKEN: ${{ secrets.HUBSPOT_ACCESS_TOKEN }}
          SENDGRID_API_KEY: ${{ secrets.SENDGRID_API_KEY }}
          DIGEST_RECIPIENTS: ${{ secrets.DIGEST_RECIPIENTS }}
          FROM_EMAIL: ${{ secrets.FROM_EMAIL }}
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
          SLACK_CHANNELS: ${{ secrets.SLACK_CHANNELS }}
          FIREFLIES_API_KEY: ${{ secrets.FIREFLIES_API_KEY }}
```

### Option 3: AWS Lambda + EventBridge

```bash
# Package for Lambda
pip install -r requirements.txt -t package/
cp followup_agent.py package/
cd package && zip -r ../deployment.zip .
```

Create Lambda function and EventBridge rule: `cron(0 9 * * ? *)`

## Customization

All customization is done through environment variables in your `.env` file:

### Finding HubSpot Deal Stage IDs (Required)

The `TARGET_STAGES` environment variable is **required**. To find your deal stage IDs:

1. Go to **HubSpot Settings → Objects → Deals → Pipelines**
2. Click on a pipeline to see its stages
3. Click on a stage name - the URL will show the stage ID (e.g., `...dealstage/12345678`)
4. Alternatively, some stages use text IDs like `appointmentscheduled` or `qualifiedtobuy`

```bash
# Example with numeric and text stage IDs
TARGET_STAGES=1930059496,appointmentscheduled,qualifiedtobuy,1647352507
```

> **Tip**: You can also find stage IDs by inspecting the network requests in your browser's developer tools when viewing deals in HubSpot.

### Change Stale Threshold

```bash
STALE_THRESHOLD_DAYS=7  # For weekly follow-ups
```

### Change Slack Channels

```bash
SLACK_CHANNELS=sales,marketing,support,deals
```

### Add/Remove Digest Recipients

```bash
DIGEST_RECIPIENTS=user1@example.com,user2@example.com,team@example.com
```

### Customize AI Prompts (Required)

⚠️ **Important**: The default prompts are configured for a specific product (Adopt AI). You **must** customize these for your own product/service.

#### 1. Email Generation Prompt

Edit the `generate_followup_email()` function in `followup_agent.py` (~line 600). Update:

- **Role & Purpose**: Change the AI's role description to match your sales context
- **Product Capabilities**: Replace the "Current Capabilities" section with your product's features
- **Email Scenarios**: Adjust the email templates for your typical sales situations
- **Tone Guidelines**: Modify to match your brand voice

```python
# Look for this section in generate_followup_email():
prompt = f"""You are a senior sales development representative...

# Update the "Current Capabilities" section:
## Current [Your Product] Capabilities
- Feature 1: Description
- Feature 2: Description
...
```

#### 2. Web Search Prompt

Edit the `search_company_news()` function in `followup_agent.py` (~line 550). Update the search query to focus on signals relevant to your product:

```python
# Current prompt searches for AI-related news
# Change to match your product's value proposition:
messages=[{
    "role": "user", 
    "content": f"Search for recent news about {company_name} related to [your relevant topics]. "
               f"Focus on: [signals that indicate buying intent for your product]..."
}]
```

#### 3. What to Customize

| Section | What to Change |
|---------|----------------|
| Product name | Replace "Adopt AI" with your product |
| Capabilities | List your product's features and benefits |
| Use cases | Describe how customers use your product |
| Search topics | What news signals buying intent for you? |
| Email tone | Match your brand voice and sales style |
| Talking points | Customize for your typical objections |

## Sample Output

The daily digest email includes:

- 📊 Total deals needing follow-up
- For each deal:
  - Deal name and pipeline stage
  - Contact name and email
  - Company name and days since last contact
  - 🔍 **Research Summary**:
    - Situation overview
    - Problems/blockers identified
    - Call insights (from Fireflies)
    - Internal insights (from Slack)
    - Web intelligence (company news)
    - Applicable product capabilities
  - 📝 **Generated Email** (subject + body)
  - 💡 **Talking Points** for responses
  - ⚠️ **Flags** and recommendations

## Troubleshooting

### No deals found?
- Verify deals exist in the target stages in HubSpot
- Check that the HubSpot token has correct scopes
- Ensure deals have associated contacts

### Emails not sending?
- Verify SendGrid API key or SMTP credentials
- Check spam folder
- Review console output for errors

### Slack/Fireflies not working?
- Verify API tokens are correct
- Check that the bot has access to the channels
- These integrations are optional - the agent works without them

### Web search failing?
- Enable web search in [Anthropic Console](https://console.anthropic.com/settings/organization/features)
- Web search is optional - emails will still generate without it

### Rate limits?
- HubSpot: 100 requests/10 seconds
- Anthropic: Check your plan limits
- Add delays if processing many deals

### Knowledge base not working?
- Run `python index_knowledge_base.py` to create the index
- Ensure documents exist in the `docs/` folder
- Check that ChromaDB is installed: `pip install chromadb sentence-transformers`

### Apollo enrichment failing?
- Verify your API key is correct
- Check Apollo API limits
- Apollo is optional - the agent works without it

## Project Structure

```
sales-ai-agents/
├── followup_agent.py           # Follow-up Agent (stale deals) - uses RAG
├── lead_finder_agent.py        # Lead Finder Agent (high-engagement contacts) - uses RAG
├── executive_summary_agent.py  # Executive Summary Agent (evening pipeline digest)
├── test_single_deal.py         # Test script for follow-up (single deal)
├── test_executive_summary.py   # Test script for executive summary (single deal)
├── index_knowledge_base.py     # Knowledge base indexer (shared by agents)
├── requirements.txt            # Python dependencies
├── .env.example                # Environment template
├── .env                        # Your configuration (gitignored)
├── docs/                       # Knowledge base documents (gitignored)
│   ├── capabilities/           # Product features and capabilities
│   ├── use-cases/              # Industry-specific use cases
│   ├── personas/               # Messaging for different job titles
│   └── case-studies/           # Customer success stories
├── .chroma/                    # ChromaDB vector database (gitignored)
└── README.md
```

## License

MIT License - feel free to use and modify for your own sales workflow.

## Contributing

Contributions welcome! Please open an issue or PR.