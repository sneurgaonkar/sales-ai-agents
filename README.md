# Sales Follow-up Email Agent

An AI-powered sales automation agent that identifies stale deals and generates personalized follow-up emails by researching prospects across multiple data sources.

> ⚠️ **Note**: This agent requires customization before use. The AI prompts are currently configured for a specific product. See the [Customize AI Prompts](#customize-ai-prompts-required) section to adapt it for your product/service.

## Features

- **CRM Integration**: Queries HubSpot for deals in specific pipeline stages
- **Multi-Source Research**: Gathers context from:
  - 📧 HubSpot emails and notes from Contact, Deal & Account objects
  - 💬 Slack internal discussions
  - 📞 Fireflies call transcripts
  - 🌐 Web search for company news & AI initiatives
- **AI-Powered Emails**: Uses Claude to generate personalized, context-rich follow-up emails
- **Daily Digest**: Sends a beautifully formatted HTML digest with all draft emails ready to review

## How It Works

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   HubSpot CRM   │────▶│  Gather Context  │────▶│  Claude AI      │
│   (Stale Deals) │     │  from all sources│     │  (Generate      │
└─────────────────┘     └──────────────────┘     │   Emails)       │
                                                  └────────┬────────┘
                                                           │
                        ┌──────────────────┐               │
                        │  Email Digest    │◀──────────────┘
                        │  (SendGrid/SMTP) │
                        └──────────────────┘
```

1. **Query HubSpot** for deals in target stages (First Meeting, Demo, Potential Fit, Cold)
2. **Filter stale deals** where the last email was sent >14 days ago
3. **Research each deal** using Slack, Fireflies, and web search
4. **Generate personalized follow-ups** using Claude with full context
5. **Send a digest email** with all draft emails ready for review

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/sneurgaonkar/sales-followup-agent.git
cd sales-followup-agent
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

### 4. Run the Agent

```bash
python followup_agent.py
```

### 5. Test with a Single Deal (Optional)

```bash
python test_single_deal.py "Deal Name"
```

## Requirements

### Python Dependencies

```
anthropic>=0.39.0
requests>=2.31.0
python-dotenv>=1.0.0
```

### API Keys & Tokens

| Service | Required | Purpose |
|---------|----------|---------|
| [Anthropic](https://console.anthropic.com/) | ✅ Yes | Claude AI for email generation |
| [HubSpot](https://developers.hubspot.com/) | ✅ Yes | CRM data (deals, contacts, companies, emails) |
| [SendGrid](https://sendgrid.com/) | ⚡ One of | Email delivery |
| SMTP Server | ⚡ One of | Email delivery (alternative to SendGrid) |
| [Slack](https://api.slack.com/) | ❌ Optional | Internal discussion search |
| [Fireflies.ai](https://fireflies.ai/) | ❌ Optional | Call transcript search |

## Configuration

### Required Environment Variables

```bash
# API Keys
ANTHROPIC_API_KEY=sk-ant-xxxxx
HUBSPOT_ACCESS_TOKEN=pat-xxxxx

# Digest Settings
DIGEST_RECIPIENTS=user1@example.com,user2@example.com
FROM_EMAIL=noreply@example.com
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
```

### Optional Customization

```bash
# HubSpot deal stages to monitor (comma-separated)
TARGET_STAGES=appointmentscheduled,qualifiedtobuy

# Days since last email to consider a deal stale
STALE_THRESHOLD_DAYS=14

# Default deal name for test script
TEST_DEAL_NAME=Test Deal
```

## Integration Setup

### HubSpot Private App

1. Go to **Settings → Integrations → Private Apps**
2. Create a new app with these scopes:
   - `crm.objects.deals.read`
   - `crm.objects.contacts.read`
   - `crm.objects.companies.read`
   - `sales-email-read`
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

### Change Target Deal Stages

Find your HubSpot deal stage IDs in **Settings → Objects → Deals → Pipelines**, then set:

```bash
TARGET_STAGES=appointmentscheduled,qualifiedtobuy,presentationscheduled
```

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

## Project Structure

```
sales-followup-agent/
├── followup_agent.py      # Main agent script
├── test_single_deal.py    # Test script for single deal
├── requirements.txt       # Python dependencies
├── .env.example          # Environment template
├── .env                  # Your configuration (gitignored)
└── README.md
```

## License

MIT License - feel free to use and modify for your own sales workflow.

## Contributing

Contributions welcome! Please open an issue or PR.
