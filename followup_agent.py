#!/usr/bin/env python3
"""
Sales Follow-up Email Agent

This script runs daily to:
1. Query HubSpot for deals in specific stages (First Meeting, Demo, Potential Fit, Cold)
2. Check if the last email sent was more than 2 weeks ago
3. Search Slack for internal context about the deal/account
4. Search Fireflies for call recording transcripts
5. Search web for company news and AI initiatives
6. Generate personalized follow-up emails using Claude
7. Send a digest email with all follow-ups to the sales team

Usage:
    python followup_agent.py

Environment Variables Required:
    ANTHROPIC_API_KEY - Your Anthropic API key
    HUBSPOT_ACCESS_TOKEN - Your HubSpot private app access token
    SENDGRID_API_KEY - Your SendGrid API key (or use SMTP settings)
    DIGEST_RECIPIENTS - Comma-separated list of email addresses for the digest
    FROM_EMAIL - Sender email address for the digest
    TARGET_STAGES - Comma-separated list of HubSpot deal stage IDs to monitor
                    (Find IDs: HubSpot Settings → Objects → Deals → Pipelines)

Optional Environment Variables:
    SLACK_BOT_TOKEN - Slack Bot OAuth token for searching internal discussions
    SLACK_CHANNELS - Comma-separated list of Slack channels to search (default: sales,marketing)
    FIREFLIES_API_KEY - Fireflies.ai API key for searching call transcripts
    PARALLEL_API_KEY - Parallel.ai API key for company web intelligence search
    STALE_THRESHOLD_DAYS - Days since last email to consider a deal stale (default: 14)
    SMTP_HOST - SMTP server host (if not using SendGrid)
    SMTP_PORT - SMTP server port
    SMTP_USER - SMTP username
    SMTP_PASSWORD - SMTP password
"""

import os
import json
import requests
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse
import anthropic
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
HUBSPOT_BASE_URL = "https://api.hubapi.com"

# Knowledge base paths
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), ".chroma")
COLLECTION_NAME = "adopt_ai_knowledge"

# Digest recipients (comma-separated in env var)
DIGEST_RECIPIENTS = [
    email.strip() 
    for email in os.getenv("DIGEST_RECIPIENTS", "").split(",") 
    if email.strip()
]
if not DIGEST_RECIPIENTS:
    raise ValueError("DIGEST_RECIPIENTS environment variable is required (comma-separated emails)")

# Sender email for digest
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@example.com")

# Deal stages to monitor (comma-separated in env var, REQUIRED)
TARGET_STAGES = [
    stage.strip() 
    for stage in os.getenv("TARGET_STAGES", "").split(",") 
    if stage.strip()
]
if not TARGET_STAGES:
    raise ValueError(
        "TARGET_STAGES environment variable is required.\n"
        "Set it to a comma-separated list of HubSpot deal stage IDs.\n"
        "Find stage IDs in HubSpot: Settings → Objects → Deals → Pipelines → click on a stage to see its ID."
    )

# Days threshold for stale deals
STALE_THRESHOLD_DAYS = int(os.getenv("STALE_THRESHOLD_DAYS", "14"))

# Slack channels to search for internal context (comma-separated in env var)
DEFAULT_SLACK_CHANNELS = "sales,marketing"
SLACK_CHANNELS = [
    channel.strip() 
    for channel in os.getenv("SLACK_CHANNELS", DEFAULT_SLACK_CHANNELS).split(",") 
    if channel.strip()
]

# Optional web search (Parallel.ai Search API)
PARALLEL_SEARCH_URL = "https://api.parallel.ai/v1beta/search"
PARALLEL_SEARCH_HEADERS = {"parallel-beta": "search-extract-2025-10-10"}


class HubSpotClient:
    """Client for HubSpot API interactions."""
    
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
    
    def search_deals(self, stages: list[str], properties: list[str]) -> list[dict]:
        """Search for deals in specific stages."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/deals/search"
        
        payload = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "dealstage",
                    "operator": "IN",
                    "values": stages
                }]
            }],
            "properties": properties,
            "limit": 100
        }
        
        all_deals = []
        after = None
        
        while True:
            if after:
                payload["after"] = after
            
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()
            
            all_deals.extend(data.get("results", []))
            
            paging = data.get("paging", {})
            if paging.get("next"):
                after = paging["next"]["after"]
            else:
                break
        
        return all_deals
    
    def get_associated_contacts(self, deal_id: str) -> list[dict]:
        """Get contacts associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/contacts"
        
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        associations = response.json().get("results", [])
        if not associations:
            return []
        
        contact_ids = [a.get("toObjectId") or a.get("id") for a in associations]
        
        # Batch read contacts
        contacts_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/batch/read"
        contacts_payload = {
            "inputs": [{"id": cid} for cid in contact_ids],
            "properties": ["email", "firstname", "lastname", "jobtitle", "company", 
                          "notes_last_contacted", "hs_sales_email_last_replied"]
        }
        
        contacts_response = requests.post(contacts_url, headers=self.headers, json=contacts_payload)
        contacts_response.raise_for_status()
        
        return contacts_response.json().get("results", [])
    
    def get_associated_company(self, deal_id: str) -> Optional[dict]:
        """Get the company associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/companies"
        
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        associations = response.json().get("results", [])
        if not associations:
            return None
        
        company_id = associations[0].get("toObjectId") or associations[0].get("id")
        
        # Get company details
        company_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/companies/{company_id}"
        company_response = requests.get(
            company_url, 
            headers=self.headers,
            params={"properties": "name,industry,numberofemployees,description,website"}
        )
        company_response.raise_for_status()
        
        return company_response.json()
    
    def get_deal_emails(self, deal_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with a deal.
        
        Fetches all email associations (with pagination) and returns the most recent ones.
        """
        return self._get_object_emails("deals", deal_id, limit)
    
    def get_company_emails(self, company_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with a company.
        
        Fetches all email associations (with pagination) and returns the most recent ones.
        """
        return self._get_object_emails("companies", company_id, limit)
    
    def _get_object_emails(self, object_type: str, object_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with any CRM object, with pagination support."""
        all_email_ids = []
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/{object_type}/{object_id}/associations/emails"
        
        # Paginate through all associations
        while url:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            
            data = response.json()
            associations = data.get("results", [])
            
            for a in associations:
                email_id = a.get("toObjectId") or a.get("id")
                if email_id:
                    all_email_ids.append(email_id)
            
            # Check for next page
            paging = data.get("paging", {})
            next_link = paging.get("next", {}).get("link")
            url = next_link if next_link else None
        
        if not all_email_ids:
            return []
        
        # Fetch all emails and sort by date to get most recent
        emails = self._fetch_emails_by_ids(all_email_ids)
        
        # Sort by timestamp (most recent first) and return limited results
        def get_email_timestamp(email):
            props = email.get("properties", {})
            ts = props.get("hs_timestamp") or props.get("hs_createdate") or "0"
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    return 0
            return ts / 1000 if ts else 0
        
        emails.sort(key=get_email_timestamp, reverse=True)
        return emails[:limit]
    
    def _fetch_emails_by_ids(self, email_ids: list[str]) -> list[dict]:
        """Fetch email details by IDs.
        
        Handles batching for large numbers of emails (HubSpot limit is 100 per batch).
        """
        if not email_ids:
            return []
        
        # Deduplicate email IDs
        unique_ids = list(set(email_ids))
        
        all_emails = []
        emails_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/emails/batch/read"
        
        # Process in batches of 100 (HubSpot limit)
        batch_size = 100
        for i in range(0, len(unique_ids), batch_size):
            batch_ids = unique_ids[i:i + batch_size]
            
            emails_payload = {
                "inputs": [{"id": eid} for eid in batch_ids],
                "properties": ["hs_email_subject", "hs_email_status", "hs_email_direction",
                              "hs_timestamp", "hs_email_text", "hs_createdate"]
            }
            
            emails_response = requests.post(emails_url, headers=self.headers, json=emails_payload)
            emails_response.raise_for_status()
            
            all_emails.extend(emails_response.json().get("results", []))
        
        return all_emails
    
    def get_deal_notes(self, deal_id: str, limit: int = 5) -> list[dict]:
        """Get notes associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/deals/{deal_id}/associations/notes"
        
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        associations = response.json().get("results", [])
        if not associations:
            return []
        
        note_ids = [a.get("toObjectId") or a.get("id") for a in associations[:limit]]
        
        # Batch read notes
        notes_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes/batch/read"
        notes_payload = {
            "inputs": [{"id": nid} for nid in note_ids],
            "properties": ["hs_note_body", "hs_timestamp", "hs_createdate"]
        }
        
        notes_response = requests.post(notes_url, headers=self.headers, json=notes_payload)
        notes_response.raise_for_status()
        
        return notes_response.json().get("results", [])


class SlackClient:
    """Client for Slack API interactions."""
    
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json"
        }
        self.base_url = "https://slack.com/api"
    
    def search_messages(self, query: str, channels: list[str], limit: int = 10) -> list[dict]:
        """Search for messages in specified channels.
        
        Args:
            query: Search query string
            channels: List of channel names to search in
            limit: Maximum number of results to return
            
        Returns:
            List of message dicts with user, text, timestamp, and channel info
        """
        # Build channel filter for the query
        channel_filter = " ".join([f"in:#{ch}" for ch in channels])
        full_query = f"{query} {channel_filter}"
        
        url = f"{self.base_url}/search.messages"
        params = {
            "query": full_query,
            "count": limit,
            "sort": "timestamp",
            "sort_dir": "desc"
        }
        
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        if not data.get("ok"):
            error = data.get("error", "Unknown error")
            print(f"   ⚠️ Slack search error: {error}")
            return []
        
        messages = data.get("messages", {}).get("matches", [])
        
        # Format messages for context
        formatted = []
        for msg in messages[:limit]:
            formatted.append({
                "text": msg.get("text", "")[:500],  # Truncate long messages
                "user": msg.get("username", msg.get("user", "Unknown")),
                "channel": msg.get("channel", {}).get("name", "unknown"),
                "timestamp": msg.get("ts", ""),
                "permalink": msg.get("permalink", "")
            })
        
        return formatted
    
    def format_slack_context(self, messages: list[dict]) -> str:
        """Format Slack messages into a readable context string for the prompt."""
        if not messages:
            return "No relevant Slack discussions found."
        
        formatted_messages = []
        for msg in messages:
            # Convert timestamp to readable date
            try:
                ts = float(msg["timestamp"])
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date_str = "Unknown date"
            
            formatted_messages.append(
                f"- [{date_str}] #{msg['channel']} - @{msg['user']}: {msg['text']}"
            )
        
        return "\n".join(formatted_messages)


def extract_domain(url: str) -> str:
    """Extract domain from URL (e.g. https://www.acme.com/path -> acme.com)."""
    if not url or not str(url).strip():
        return ""
    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or ""
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.lower() if netloc else ""
    except Exception:
        return ""


class FirefliesClient:
    """Client for Fireflies.ai API interactions."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.fireflies.ai/graphql"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    
    def search_transcripts_by_title(self, search_term: str, limit: int = 5) -> list[dict]:
        """Search for meeting transcripts by title (account/company name).
        
        Args:
            search_term: Account or company name to search for in meeting titles
            limit: Maximum number of transcripts to return
            
        Returns:
            List of transcript dicts with id, title, date, summary, etc.
        """
        query = """
        query TranscriptsByTitle($title: String!, $limit: Int) {
            transcripts(title: $title, limit: $limit) {
                id
                title
                date
                duration
                summary {
                    overview
                    action_items
                    keywords
                }
            }
        }
        """
        
        variables = {
            "title": search_term,
            "limit": limit
        }
        
        try:
            response = requests.post(
                self.base_url,
                headers=self.headers,
                json={"query": query, "variables": variables}
            )
            response.raise_for_status()
            
            data = response.json()
            
            if "errors" in data:
                print(f"   ⚠️ Fireflies API error: {data['errors']}")
                return []
            
            return data.get("data", {}).get("transcripts", []) or []
            
        except requests.exceptions.RequestException as e:
            print(f"   ⚠️ Fireflies request error: {e}")
            return []
    
    def get_transcript_details(self, transcript_id: str) -> Optional[dict]:
        """Get detailed transcript including sentences/content.
        
        Args:
            transcript_id: The Fireflies transcript ID
            
        Returns:
            Transcript dict with full details including sentences
        """
        query = """
        query TranscriptDetails($id: String!) {
            transcript(id: $id) {
                id
                title
                date
                duration
                summary {
                    overview
                    action_items
                    keywords
                    shorthand_bullet
                }
                sentences {
                    text
                    speaker_name
                    start_time
                }
            }
        }
        """
        
        variables = {"id": transcript_id}
        
        try:
            response = requests.post(
                self.base_url,
                headers=self.headers,
                json={"query": query, "variables": variables}
            )
            response.raise_for_status()
            
            data = response.json()
            
            if "errors" in data:
                print(f"   ⚠️ Fireflies API error: {data['errors']}")
                return None
            
            return data.get("data", {}).get("transcript")
            
        except requests.exceptions.RequestException as e:
            print(f"   ⚠️ Fireflies request error: {e}")
            return None
    
    def format_fireflies_context(self, transcripts: list[dict]) -> str:
        """Format Fireflies transcripts into a readable context string for the prompt.
        
        Args:
            transcripts: List of transcript dicts from search_transcripts_by_email
            
        Returns:
            Formatted string with meeting summaries and key points
        """
        if not transcripts:
            return "No call transcripts found for this contact."
        
        formatted_transcripts = []
        
        for transcript in transcripts:
            # Parse date - Fireflies can return date as string or timestamp
            date_val = transcript.get("date", "Unknown date")
            date_str = "Unknown date"
            if date_val:
                try:
                    if isinstance(date_val, (int, float)):
                        # Unix timestamp (seconds or milliseconds)
                        ts = date_val / 1000 if date_val > 1e12 else date_val
                        dt = datetime.fromtimestamp(ts)
                        date_str = dt.strftime("%Y-%m-%d")
                    elif isinstance(date_val, str):
                        # ISO format string
                        dt = datetime.fromisoformat(date_val.replace("Z", "+00:00"))
                        date_str = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError, OSError):
                    date_str = str(date_val) if date_val else "Unknown date"
            
            title = transcript.get("title", "Untitled Meeting")
            duration = transcript.get("duration", 0)
            duration_mins = round(duration / 60) if duration else 0
            
            summary = transcript.get("summary", {}) or {}
            overview = summary.get("overview", "No summary available")
            action_items = summary.get("action_items", []) or []
            keywords = summary.get("keywords", []) or []
            
            # Build transcript summary
            transcript_text = f"📞 **{title}** ({date_str}, {duration_mins} mins)\n"
            transcript_text += f"   Summary: {overview[:500]}...\n" if len(overview) > 500 else f"   Summary: {overview}\n"
            
            if action_items:
                transcript_text += f"   Action Items: {', '.join(action_items[:5])}\n"
            
            if keywords:
                transcript_text += f"   Keywords: {', '.join(keywords[:10])}\n"
            
            formatted_transcripts.append(transcript_text)
        
        return "\n".join(formatted_transcripts)


class KnowledgeBaseClient:
    """Client for querying the Adopt AI knowledge base via ChromaDB."""
    
    def __init__(self):
        self.client = None
        self.collection = None
        self._initialized = False
        
    def initialize(self) -> bool:
        """Initialize ChromaDB connection. Returns True if successful."""
        if self._initialized:
            return True
            
        try:
            import chromadb
            
            if not os.path.exists(CHROMA_PERSIST_DIR):
                print("   ⚠️ Knowledge base not found. Run 'python index_knowledge_base.py' first.")
                return False
            
            self.client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
            self.collection = self.client.get_collection(COLLECTION_NAME)
            self._initialized = True
            return True
            
        except ImportError:
            print("   ⚠️ ChromaDB not installed. Run 'pip install chromadb'")
            return False
        except ValueError as e:
            print(f"   ⚠️ Knowledge base collection not found: {e}")
            print("   Run 'python index_knowledge_base.py' to create it.")
            return False
        except Exception as e:
            print(f"   ⚠️ Error initializing knowledge base: {e}")
            return False
    
    def search(self, query: str, n_results: int = 5) -> list[dict]:
        """Search the knowledge base for relevant content."""
        if not self._initialized and not self.initialize():
            return []
        
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results
            )
            
            if not results["documents"] or not results["documents"][0]:
                return []
            
            formatted_results = []
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                formatted_results.append({
                    "content": doc,
                    "source": meta.get("source", "unknown"),
                    "category": meta.get("category", "general"),
                })
            
            return formatted_results
            
        except Exception as e:
            print(f"   ⚠️ Knowledge base search error: {e}")
            return []
    
    def get_context_for_deal(self, deal_context: dict) -> str:
        """Build a comprehensive knowledge base context for a specific deal.
        
        Searches for:
        1. Industry-specific use cases and capabilities
        2. Persona-relevant messaging (based on job title)
        3. Company size-appropriate value propositions
        """
        if not self._initialized and not self.initialize():
            return "Knowledge base not available."
        
        all_results = []
        
        # Search by industry
        industry = deal_context.get("company_industry", "")
        if industry and industry != "Unknown":
            results = self.search(f"{industry} industry use case capabilities", n_results=3)
            all_results.extend(results)
        
        # Search by job title/persona
        job_title = deal_context.get("contact_title", "")
        if job_title and job_title != "Unknown":
            results = self.search(f"{job_title} persona messaging value proposition", n_results=3)
            all_results.extend(results)
        
        # Search based on notes/context for specific problems
        notes = deal_context.get("notes", "")
        if notes and notes != "No notes available":
            # Extract key terms from notes
            results = self.search(f"solution for {notes[:200]}", n_results=2)
            all_results.extend(results)
        
        # General capabilities search
        results = self.search("Adopt AI capabilities features platform", n_results=2)
        all_results.extend(results)
        
        # Deduplicate and format
        seen_content = set()
        unique_results = []
        for r in all_results:
            content_hash = hash(r["content"][:100])
            if content_hash not in seen_content:
                seen_content.add(content_hash)
                unique_results.append(r)
        
        return self.format_kb_context(unique_results[:8])
    
    def format_kb_context(self, results: list[dict]) -> str:
        """Format knowledge base results into a context string for the prompt."""
        if not results:
            return "No relevant knowledge base content found."
        
        formatted = []
        for r in results:
            source = r.get("source", "unknown")
            category = r.get("category", "general")
            content = r.get("content", "")[:800]
            
            formatted.append(f"[{category}/{source}]\n{content}")
        
        return "\n\n---\n\n".join(formatted)


def get_last_sent_email_date(emails: list[dict], verbose: bool = False) -> Optional[datetime]:
    """Extract the most recent SENT email date from a list of emails.
    
    Checks multiple HubSpot email properties to identify outbound emails:
    - hs_email_status: SENT
    - hs_email_direction: EMAIL (outbound) vs INCOMING_EMAIL (inbound)
    """
    sent_emails = []
    
    for email in emails:
        props = email.get("properties", {})
        status = props.get("hs_email_status", "")
        direction = props.get("hs_email_direction", "")
        subject = props.get("hs_email_subject", "")[:50] if props.get("hs_email_subject") else "No subject"
        
        # Look for outbound sent emails
        # HubSpot uses "EMAIL" for outbound and "INCOMING_EMAIL" for inbound
        is_sent = (
            status == "SENT" or 
            direction == "EMAIL" or 
            direction == "OUTGOING_EMAIL" or
            (status and status != "BOUNCED" and direction != "INCOMING_EMAIL")
        )
        
        if verbose:
            print(f"      📧 [{status}/{direction}] {subject}")
        
        if is_sent:
            timestamp = props.get("hs_timestamp") or props.get("hs_createdate")
            if timestamp:
                try:
                    # Handle millisecond timestamps
                    if isinstance(timestamp, str):
                        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromtimestamp(timestamp / 1000)
                    sent_emails.append(dt)
                except (ValueError, TypeError):
                    continue
    
    return max(sent_emails) if sent_emails else None


def is_deal_stale(last_email_date: Optional[datetime], threshold_days: int = STALE_THRESHOLD_DAYS) -> bool:
    """Check if a deal is stale based on last email date."""
    if not last_email_date:
        return True  # No emails = definitely stale
    
    # Make comparison timezone-aware or naive consistently
    now = datetime.now(last_email_date.tzinfo) if last_email_date.tzinfo else datetime.now()
    cutoff = now - timedelta(days=threshold_days)
    
    return last_email_date < cutoff


def search_company_news(company_name: str, parallel_api_key: str, max_results: int = 3) -> str:
    """Search company intelligence via Parallel.ai Search API."""
    if not company_name or company_name == "Unknown Company":
        return "No company name available for web search."
    if not parallel_api_key:
        return "Web search not configured (PARALLEL_API_KEY not set)."

    objective = (
        f'{company_name} AI initiatives leadership hires digital transformation '
        f'technology partnerships funding acquisitions strategic priorities'
    )

    try:
        response = requests.post(
            PARALLEL_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": parallel_api_key,
                **PARALLEL_SEARCH_HEADERS,
            },
            json={
                "objective": objective,
                "search_queries": [objective],
                "max_results": max_results,
                "excerpts": {"max_chars_per_result": 1000},
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        snippets = []
        for result in data.get("results", [])[:max_results]:
            title = (result.get("title") or "").strip()
            excerpts = result.get("excerpts") or []
            excerpt = excerpts[0].strip() if excerpts else ""
            if excerpt and len(excerpt) > 800:
                excerpt = excerpt[:800] + "..."
            if title or excerpt:
                snippets.append(f"{title}: {excerpt}".strip(": "))

        return "\n\n".join(snippets) if snippets else "No relevant news found."
    except Exception as e:
        return f"Web search unavailable: {str(e)}"


def generate_followup_email(client: anthropic.Anthropic, deal_context: dict) -> dict:
    """Use Claude to generate a personalized follow-up email."""
    
    # Determine if deal has been cold for 6+ months
    days_since = deal_context['days_since_contact']
    is_very_cold = isinstance(days_since, int) and days_since > 180
    
    prompt = f"""## Role & Purpose

You are an AI sales assistant for Adopt AI, specializing in generating personalized, context-rich follow-up emails for sales opportunities. Your goal is to help the sales team re-engage prospects by connecting their specific problems to new product capabilities or relevant success stories from similar customers.

## Deal Context (from HubSpot CRM)

**Deal Name:** {deal_context['deal_name']}
**Stage:** {deal_context['stage']}
**Days Since Last Contact:** {deal_context['days_since_contact']}

**Contact:**
- Name: {deal_context['contact_name']}
- Title: {deal_context['contact_title']}
- Email: {deal_context['contact_email']}

**Company:**
- Name: {deal_context['company_name']}
- Industry: {deal_context['company_industry']}
- Size: {deal_context['company_size']}

**Recent Notes (may contain problems, blockers, or objections):**
{deal_context['notes']}

**Last Email Subject:**
{deal_context['last_email_subject']}

**Internal Slack Discussions (from #sales, #marketing, #designpartners):**
{deal_context.get('slack_context', 'No Slack context available.')}

**Call Recording Transcripts (from Fireflies):**
{deal_context.get('fireflies_context', 'No call transcripts available.')}

**Recent Company News & Intelligence (from web search):**
{deal_context.get('web_research', 'No web research available.')}

## Adopt AI Knowledge Base (Relevant Context)

The following content was retrieved from our knowledge base based on this deal's industry, persona, and context. Use this to craft a more personalized and relevant email:

{deal_context.get('knowledge_base_context', 'No knowledge base content available.')}

## Your Task

### Step 1: Analyze the Context
From the notes, Slack discussions, call transcripts, web research, and deal information above, identify:
1. **Problems/Blockers**: What did they need that we couldn't deliver? Why did the deal stall? Look for "we can't do that yet" moments in Slack and specific pain points mentioned in calls.
2. **Call Insights**: What specific problems, feature requests, or objections were mentioned in call recordings? What business outcomes were they hoping to achieve?
3. **New Capabilities That Apply**: What's changed in our product that addresses their needs?
4. **Internal Insights**: What did the team discuss internally about this deal? Any technical limitations or product feedback mentioned?
5. **Web Intelligence**: Any recent AI initiatives, leadership hires, or strategic moves that create an opening for Adopt?
6. **Similar Deal Insights**: What use cases or outcomes from comparable customers might resonate?

### Step 2: Determine Email Approach

**Scenario A - They had a specific unmet need:**
- Lead with: "When we last spoke, you mentioned needing [X]. Wanted to share that we now [capability]."
- Focus on how the new feature solves their exact problem

**Scenario B - Deal went cold without clear blocker:**
- Lead with: "Circling back—I came across [relevant use case/outcome] that reminded me of your goals around [X]."
- Share a brief success story or business outcome from similar customers

### Step 3: Generate the Email

**Email Structure (80-120 words MAX):**
1. **Subject Line**: Reference their specific problem or use case
2. **Opening**: Brief context reconnection—ONE sentence referencing the specific problem or blocker
3. **The "Now We Can" Moment**: This is the core. Explain what's changed:
   - If they had a specific problem → show how new capabilities address it
   - If deal just went cold → share a relevant use case or business outcome
4. **Simple CTA**: One clear ask (suggest a call if deal has been cold 6+ months)

**Tone Guidelines:**
- Helpful, not salesy
- Show you remember their specific situation
- Focus on THEIR problems, not our features
- Concise and respectful of their time
- The email is about THEM, not us

{"**IMPORTANT: This deal has been cold for 6+ months. Recommend a call instead of email as primary outreach, or acknowledge the long gap directly.**" if is_very_cold else ""}

## Response Format

Respond with JSON in this exact format:
{{
    "research_summary": {{
        "their_situation": "Brief summary of their context and last touchpoint",
        "problems_blockers": "What they needed or why the deal stalled",
        "call_insights": "Key points from call recordings - pain points, feature requests, objections mentioned",
        "internal_insights": "Key points from internal Slack discussions (if any)",
        "web_insights": "Relevant company news, AI initiatives, or leadership changes to leverage",
        "applicable_capabilities": "New capabilities that address their needs",
        "similar_insights": "Relevant use cases or outcomes to reference"
    }},
    "subject": "Email subject line referencing their specific problem",
    "body": "The email body (80-120 words max)"
}}
"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    # Parse the response
    response_text = response.content[0].text
    
    # Try to extract JSON from the response
    try:
        # Handle potential markdown code blocks
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text.strip()
        
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {
            "research_summary": {
                "their_situation": "Unable to parse - see raw response",
                "problems_blockers": "Unknown",
                "applicable_capabilities": "Unknown",
                "similar_insights": "None identified"
            },
            "subject": "Following up on our conversation",
            "body": response_text
        }


def format_digest_html(followups: list[dict]) -> str:
    """Format all follow-ups into an HTML digest email."""
    
    date_str = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    count = len(followups)
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 30px; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .header p {{ margin: 10px 0 0 0; opacity: 0.9; }}
        .deal-card {{ background: #f8f9fa; border-radius: 10px; padding: 25px; margin-bottom: 25px; border-left: 4px solid #667eea; }}
        .deal-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }}
        .deal-name {{ font-size: 18px; font-weight: 600; color: #333; }}
        .deal-stage {{ background: #667eea; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; }}
        .deal-meta {{ color: #666; font-size: 14px; margin-bottom: 15px; }}
        .email-preview {{ background: white; border-radius: 8px; padding: 20px; margin-top: 15px; }}
        .email-subject {{ font-weight: 600; color: #333; margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        .email-body {{ white-space: pre-wrap; color: #444; }}
        .research-summary {{ background: #e8f4f8; border-radius: 8px; padding: 15px; margin: 15px 0; border-left: 3px solid #17a2b8; }}
        .research-summary h4 {{ margin: 0 0 10px 0; color: #17a2b8; font-size: 13px; text-transform: uppercase; }}
        .research-item {{ font-size: 13px; color: #555; margin-bottom: 8px; line-height: 1.5; }}
        .research-item strong {{ color: #333; }}
        .contact-info {{ font-size: 13px; color: #888; }}
        .footer {{ text-align: center; color: #999; font-size: 12px; margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; }}
        .no-followups {{ text-align: center; padding: 40px; color: #666; }}
        .stats {{ display: flex; gap: 20px; margin-top: 15px; }}
        .stat {{ background: rgba(255,255,255,0.2); padding: 10px 15px; border-radius: 8px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; }}
        .stat-label {{ font-size: 12px; opacity: 0.9; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>📧 Daily Follow-up Digest</h1>
        <p>Generated on {date_str}</p>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{count}</div>
                <div class="stat-label">Deals Need Follow-up</div>
            </div>
        </div>
    </div>
"""
    
    if not followups:
        html += """
    <div class="no-followups">
        <h2>🎉 All caught up!</h2>
        <p>No deals require follow-up today. All active deals have been contacted within the last 14 days.</p>
    </div>
"""
    else:
        for fu in followups:
            # Build research summary HTML if available
            research_html = ""
            research = fu.get("research_summary", {})
            if research:
                call_insights = research.get('call_insights', 'N/A')
                call_insights_html = f"<div class='research-item'><strong>📞 Call Insights (Fireflies):</strong> {call_insights}</div>" if call_insights and call_insights != 'N/A' else ""
                
                internal_insights = research.get('internal_insights', 'N/A')
                internal_insights_html = f"<div class='research-item'><strong>💬 Internal Insights (Slack):</strong> {internal_insights}</div>" if internal_insights and internal_insights != 'N/A' else ""
                
                web_insights = research.get('web_insights', 'N/A')
                web_insights_html = f"<div class='research-item'><strong>🌐 Web Intelligence:</strong> {web_insights}</div>" if web_insights and web_insights != 'N/A' else ""
                
                research_html = f"""
                <div class="research-summary">
                    <h4>🔍 Research Summary</h4>
                    <div class="research-item"><strong>Situation:</strong> {research.get('their_situation', 'N/A')}</div>
                    <div class="research-item"><strong>Problems/Blockers:</strong> {research.get('problems_blockers', 'N/A')}</div>
                    {call_insights_html}
                    {internal_insights_html}
                    {web_insights_html}
                    <div class="research-item"><strong>Applicable Capabilities:</strong> {research.get('applicable_capabilities', 'N/A')}</div>
                    <div class="research-item"><strong>Similar Insights:</strong> {research.get('similar_insights', 'N/A')}</div>
                </div>
"""
            
            html += f"""
    <div class="deal-card">
        <div class="deal-header">
            <span class="deal-name">{fu['deal_name']}</span>
            <span class="deal-stage">{fu['stage']}</span>
        </div>
        <div class="deal-meta">
            <strong>{fu['contact_name']}</strong> ({fu['contact_email']})<br>
            {fu['company_name']} • Last contact: {fu['days_since_contact']} days ago
        </div>
        {research_html}
        <div class="email-preview">
            <div class="email-subject">📝 Subject: {fu['email_subject']}</div>
            <div class="email-body">{fu['email_body']}</div>
        </div>
    </div>
"""
    
    html += """
    <div class="footer">
        <p>This digest was automatically generated by your HubSpot Follow-up Agent.<br>
        Review each email before sending and personalize as needed.</p>
    </div>
</body>
</html>
"""
    
    return html


def send_digest_email_sendgrid(to_emails: list[str], html_content: str, api_key: str):
    """Send the digest email using SendGrid."""
    url = "https://api.sendgrid.com/v3/mail/send"
    
    payload = {
        "personalizations": [{"to": [{"email": email} for email in to_emails]}],
        "from": {"email": FROM_EMAIL, "name": "Follow-up Agent"},
        "subject": f"📧 Daily Follow-up Digest - {datetime.now().strftime('%B %d, %Y')}",
        "content": [{"type": "text/html", "value": html_content}]
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    
    print(f"✅ Digest email sent to {', '.join(to_emails)}")


def send_digest_email_smtp(to_emails: list[str], html_content: str):
    """Send the digest email using SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("SMTP_FROM_EMAIL", smtp_user)
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📧 Daily Follow-up Digest - {datetime.now().strftime('%B %d, %Y')}"
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    
    msg.attach(MIMEText(html_content, "html"))
    
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_email, to_emails, msg.as_string())
    
    print(f"✅ Digest email sent to {', '.join(to_emails)}")


def main():
    """Main execution flow."""
    print("🚀 Starting HubSpot Follow-up Agent...")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 50)
    
    # Initialize clients
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    sendgrid_key = os.getenv("SENDGRID_API_KEY")
    parallel_key = os.getenv("PARALLEL_API_KEY", "")
    
    if not hubspot_token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is required")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")
    
    hubspot = HubSpotClient(hubspot_token)
    claude = anthropic.Anthropic(api_key=anthropic_key)
    
    # Initialize Slack client if token is available
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack = SlackClient(slack_token) if slack_token else None
    if slack:
        print(f"🔗 Slack integration enabled (searching: #{', #'.join(SLACK_CHANNELS)})")
    else:
        print("ℹ️ Slack integration disabled (SLACK_BOT_TOKEN not set)")
    
    # Initialize Fireflies client if API key is available
    fireflies_key = os.getenv("FIREFLIES_API_KEY")
    fireflies = FirefliesClient(fireflies_key) if fireflies_key else None
    if fireflies:
        print(f"📞 Fireflies integration enabled (searching call transcripts)")
    else:
        print("ℹ️ Fireflies integration disabled (FIREFLIES_API_KEY not set)")
    
    # Initialize knowledge base client
    knowledge_base = KnowledgeBaseClient()
    if knowledge_base.initialize():
        print(f"📚 Knowledge base enabled ({knowledge_base.collection.count()} chunks indexed)")
    else:
        print("ℹ️ Knowledge base disabled (run 'python index_knowledge_base.py' to enable)")
        knowledge_base = None
    
    # Step 1: Get deals in target stages
    print(f"\n📊 Fetching deals in stages: {', '.join(TARGET_STAGES)}")
    
    deals = hubspot.search_deals(
        stages=TARGET_STAGES,
        properties=["dealname", "dealstage", "amount", "closedate", 
                   "notes_last_contacted", "hs_lastmodifieddate"]
    )
    
    print(f"   Found {len(deals)} deals in target stages")
    
    # Step 2: Filter to stale deals and gather context
    stale_deals = []
    
    for deal in deals:
        deal_id = deal["id"]
        deal_name = deal["properties"].get("dealname", "Unknown Deal")
        stage = deal["properties"].get("dealstage", "")
        stage_label = stage  # Use stage ID as label
        
        print(f"\n🔍 Checking: {deal_name} ({stage_label})")
        
        # Get associated company first (needed for company email check)
        company = hubspot.get_associated_company(deal_id)
        company_id = company.get("id") if company else None
        company_name = company.get("properties", {}).get("name", "Unknown") if company else "Unknown"
        
        # Get associated emails from deal
        deal_emails = hubspot.get_deal_emails(deal_id)
        print(f"   📁 Deal emails found: {len(deal_emails)}")
        last_deal_email_date = get_last_sent_email_date(deal_emails)
        
        # Get emails from company (account) object
        company_emails = []
        last_company_email_date = None
        if company_id:
            company_emails = hubspot.get_company_emails(company_id)
            print(f"   🏢 Company ({company_name}) emails found: {len(company_emails)}")
            last_company_email_date = get_last_sent_email_date(company_emails)
        else:
            print(f"   ⚠️ No company associated with this deal")
        
        # Deduplicate emails (same email might be associated with both deal and company)
        seen_email_ids = set()
        unique_emails = []
        for email in deal_emails + company_emails:
            email_id = email.get("id")
            if email_id and email_id not in seen_email_ids:
                seen_email_ids.add(email_id)
                unique_emails.append(email)
        
        if len(unique_emails) != len(deal_emails) + len(company_emails):
            print(f"   📊 After deduplication: {len(unique_emails)} unique emails")
        
        # Use the most recent email date from either deal or company
        last_email_date = None
        email_source = None
        
        if last_deal_email_date and last_company_email_date:
            if last_deal_email_date >= last_company_email_date:
                last_email_date = last_deal_email_date
                email_source = "deal"
            else:
                last_email_date = last_company_email_date
                email_source = "company"
        elif last_deal_email_date:
            last_email_date = last_deal_email_date
            email_source = "deal"
        elif last_company_email_date:
            last_email_date = last_company_email_date
            email_source = "company"
        
        # Store deduplicated emails for later use
        emails = unique_emails
        
        if last_email_date:
            days_ago = (datetime.now(last_email_date.tzinfo) - last_email_date).days
            print(f"   ✉️ Last sent email: {days_ago} days ago (from {email_source})")
        else:
            days_ago = 999
            print(f"   ❌ No sent emails found (checked deal and company)")
        
        # Check if stale
        if not is_deal_stale(last_email_date):
            print(f"   ✓ Recently contacted, skipping")
            continue
        
        print(f"   ⚠️ Needs follow-up!")
        
        # Gather full context (company already fetched above)
        contacts = hubspot.get_associated_contacts(deal_id)
        notes = hubspot.get_deal_notes(deal_id)
        
        # Get primary contact
        contact = contacts[0] if contacts else {}
        contact_props = contact.get("properties", {})
        
        # Get company info
        company_props = company.get("properties", {}) if company else {}
        
        # Format notes
        notes_text = "\n".join([
            n.get("properties", {}).get("hs_note_body", "")[:500] 
            for n in notes[:3]
        ]) or "No notes available"
        
        # Get last email subject
        last_email_subject = "No previous emails"
        if emails:
            for email in emails:
                if email.get("properties", {}).get("hs_email_status") == "SENT":
                    last_email_subject = email.get("properties", {}).get("hs_email_subject", "No subject")
                    break
        
        # Search Slack for internal context
        slack_context = "Slack integration not enabled."
        if slack:
            company_name = company_props.get("name", "")
            contact_name = f"{contact_props.get('firstname', '')} {contact_props.get('lastname', '')}".strip()
            
            # Build search queries - search for company name, deal name, and contact name
            search_terms = [t for t in [company_name, deal_name, contact_name] if t and t != "Unknown"]
            
            all_slack_messages = []
            for term in search_terms[:2]:  # Limit to 2 searches to avoid rate limits
                try:
                    messages = slack.search_messages(term, SLACK_CHANNELS, limit=5)
                    all_slack_messages.extend(messages)
                except Exception as e:
                    print(f"   ⚠️ Slack search error for '{term}': {e}")
            
            # Deduplicate messages by permalink
            seen_permalinks = set()
            unique_messages = []
            for msg in all_slack_messages:
                if msg.get("permalink") not in seen_permalinks:
                    seen_permalinks.add(msg.get("permalink"))
                    unique_messages.append(msg)
            
            slack_context = slack.format_slack_context(unique_messages[:10])
            if unique_messages:
                print(f"   💬 Found {len(unique_messages)} Slack messages")
        
        # Search Fireflies for call transcripts by account name and company domain
        fireflies_context = "Fireflies integration not enabled."
        account_name = company_props.get("name", "")
        company_domain = extract_domain(company_props.get("website") or "")
        if fireflies and (account_name and account_name != "Unknown Company" or company_domain):
            search_terms = [t for t in [account_name, company_domain] if t and t != "Unknown Company"]
            seen_ids = set()
            all_transcripts = []
            for term in search_terms[:2]:
                try:
                    txs = fireflies.search_transcripts_by_title(term, limit=5)
                    for t in txs:
                        tid = t.get("id")
                        if tid and tid not in seen_ids:
                            seen_ids.add(tid)
                            all_transcripts.append(t)
                except Exception as e:
                    print(f"   ⚠️ Fireflies search error for '{term}': {e}")
            if all_transcripts:
                fireflies_context = fireflies.format_fireflies_context(all_transcripts[:5])
                print(f"   ✓ Found {len(all_transcripts)} call transcript(s) (searched: {', '.join(search_terms)})")
            else:
                fireflies_context = "No call transcripts found for this account."
                print(f"   ℹ️ No call transcripts found")
        elif fireflies:
            fireflies_context = "No account name or domain available for Fireflies search."
        
        # Web search for company news and intelligence
        web_research = "Web search not performed."
        company_name_for_search = company_props.get("name", "")
        if company_name_for_search and company_name_for_search != "Unknown Company":
            print(f"   🌐 Searching web for {company_name_for_search}...")
            try:
                web_research = search_company_news(company_name_for_search, parallel_key)
                if web_research and "No relevant news" not in web_research:
                    print(f"   ✓ Found web intelligence")
                else:
                    print(f"   ℹ️ No relevant web news found")
            except Exception as e:
                print(f"   ⚠️ Web search error: {e}")
                web_research = f"Web search failed: {str(e)}"
        
        deal_context = {
            "deal_id": deal_id,
            "deal_name": deal_name,
            "stage": stage_label,
            "days_since_contact": days_ago if days_ago < 999 else "30+",
            "contact_name": f"{contact_props.get('firstname', '')} {contact_props.get('lastname', '')}".strip() or "Unknown",
            "contact_email": contact_props.get("email", "No email"),
            "contact_title": contact_props.get("jobtitle", "Unknown"),
            "company_name": company_props.get("name", "Unknown Company"),
            "company_industry": company_props.get("industry", "Unknown"),
            "company_size": company_props.get("numberofemployees", "Unknown"),
            "notes": notes_text,
            "last_email_subject": last_email_subject,
            "slack_context": slack_context,
            "fireflies_context": fireflies_context,
            "web_research": web_research,
        }
        
        # Knowledge base context (RAG)
        kb_context = "Knowledge base not available."
        if knowledge_base:
            print(f"   📚 Searching knowledge base...")
            kb_context = knowledge_base.get_context_for_deal(deal_context)
            if kb_context and "No relevant" not in kb_context and "not available" not in kb_context:
                print(f"   ✓ Found relevant knowledge base content")
            else:
                print(f"   ℹ️ No specific knowledge base matches")
        
        deal_context["knowledge_base_context"] = kb_context
        
        stale_deals.append(deal_context)
    
    print(f"\n" + "=" * 50)
    print(f"📝 Found {len(stale_deals)} deals needing follow-up")
    
    # Step 3: Generate follow-up emails
    followups = []
    
    for deal_ctx in stale_deals:
        print(f"\n✍️ Generating email for: {deal_ctx['deal_name']}")
        
        try:
            email_content = generate_followup_email(claude, deal_ctx)
            
            followups.append({
                **deal_ctx,
                "email_subject": email_content.get("subject", "Follow-up"),
                "email_body": email_content.get("body", ""),
                "research_summary": email_content.get("research_summary", {})
            })
            
            print(f"   ✓ Generated: {email_content.get('subject', 'No subject')[:50]}...")
            
        except Exception as e:
            print(f"   ❌ Error generating email: {e}")
            continue
    
    # Step 4: Create and send digest
    print(f"\n📧 Creating digest email...")
    
    html_digest = format_digest_html(followups)
    
    # Save a local copy
    digest_path = f"/tmp/followup_digest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    with open(digest_path, "w") as f:
        f.write(html_digest)
    print(f"   📄 Saved local copy: {digest_path}")
    
    # Send the email
    if sendgrid_key:
        send_digest_email_sendgrid(DIGEST_RECIPIENTS, html_digest, sendgrid_key)
    elif os.getenv("SMTP_HOST"):
        send_digest_email_smtp(DIGEST_RECIPIENTS, html_digest)
    else:
        print(f"\n⚠️ No email service configured. Digest saved to: {digest_path}")
        print(f"   Set SENDGRID_API_KEY or SMTP_* environment variables to enable email delivery.")
    
    print(f"\n✅ Agent completed successfully!")
    print(f"   Deals checked: {len(deals)}")
    print(f"   Follow-ups generated: {len(followups)}")
    
    return followups


if __name__ == "__main__":
    main()
