#!/usr/bin/env python3
"""
Lead Finder AI Agent

This script identifies high-engagement but stale leads in HubSpot (Marketing Contacts without deals)
and generates personalized outreach by enriching context from multiple sources. For each lead it
produces both an email and a short LinkedIn connection note (for use when sending a connection request).

Workflow:
1. Query HubSpot for Marketing Contacts (hs_marketable_status = true)
2. Exclude contacts already processed (tracked in processed_contacts_log.json)
3. Apply configurable filters (employee size, industry, country, job title, lifecycle stage)
4. Score contacts by engagement (HubSpot lead scoring or custom: email opens/clicks, page views, etc.)
5. Filter for stale contacts (no activity in STALE_THRESHOLD_DAYS; optional)
6. Rank and select top N leads (N = 20), split between assigned recipients (10 each)
7. Enrich with context from Apollo, Slack, and Fireflies, and knowledge base (RAG)
8. Generate personalized outreach emails using Claude
9. Generate LinkedIn connection notes (max 300 characters) for each lead using Claude
10. Send a digest email to the sales team with emails and LinkedIn notes

Usage:
    python lead_finder_agent.py

Environment Variables Required:
    ANTHROPIC_API_KEY - Your Anthropic API key
    HUBSPOT_ACCESS_TOKEN - Your HubSpot private app access token
    SENDGRID_API_KEY - Your SendGrid API key
    LEAD_FINDER_RECIPIENTS - Comma-separated list of email addresses for the digest
    FROM_EMAIL - Sender email address for the digest

Optional Environment Variables:
    TOP_LEADS_COUNT - Total number of top leads to process (default: 20 for 10 each)
    APOLLO_API_KEY - Apollo.io API key for contact/company enrichment
    SLACK_BOT_TOKEN - Slack Bot OAuth token for searching internal discussions
    SLACK_CHANNELS - Comma-separated list of Slack channels to search
    FIREFLIES_API_KEY - Fireflies.ai API key for searching call transcripts
    PARALLEL_API_KEY - Parallel.ai API key for company web intelligence search
    USE_HUBSPOT_SCORING - Use HubSpot lead_scoring_* properties (default: true); false = custom engagement score
    
Contact Filtering (all optional):
    MIN_EMPLOYEE_SIZE - Minimum company employee size (default: 200)
    TARGET_INDUSTRIES - Comma-separated list of industries to include
    TARGET_COUNTRIES - Comma-separated list of countries to include
    TARGET_JOB_TITLES - Comma-separated job title keywords to include
    TARGET_LIFECYCLE_STAGES - Comma-separated HubSpot lifecycle stages to include
    STALE_THRESHOLD_DAYS - Days since last contact to consider stale (default: 14)
"""

import os
import json
import html as html_module
import requests
import random
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
LEAD_FINDER_RECIPIENTS = [
    email.strip() 
    for email in os.getenv("LEAD_FINDER_RECIPIENTS", "").split(",") 
    if email.strip()
]
if not LEAD_FINDER_RECIPIENTS:
    raise ValueError("LEAD_FINDER_RECIPIENTS environment variable is required (comma-separated emails)")

# Sender email for digest
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@example.com")

# Days threshold for stale contacts (optional, default: 14)
STALE_THRESHOLD_DAYS = int(os.getenv("STALE_THRESHOLD_DAYS", "14")) if os.getenv("STALE_THRESHOLD_DAYS") else None

# Contact filtering configuration (all optional)
MIN_EMPLOYEE_SIZE = int(os.getenv("MIN_EMPLOYEE_SIZE", "0")) if os.getenv("MIN_EMPLOYEE_SIZE") else 0

TARGET_INDUSTRIES = [
    ind.strip() 
    for ind in os.getenv("TARGET_INDUSTRIES", "").split(",") 
    if ind.strip()
]

TARGET_COUNTRIES = [
    country.strip() 
    for country in os.getenv("TARGET_COUNTRIES", "").split(",") 
    if country.strip()
]

TARGET_JOB_TITLES = [
    title.strip() 
    for title in os.getenv("TARGET_JOB_TITLES", "").split(",") 
    if title.strip()
]

TARGET_LIFECYCLE_STAGES = [
    stage.strip().lower() 
    for stage in os.getenv("TARGET_LIFECYCLE_STAGES", "").split(",") 
    if stage.strip()
]

# Slack channels to search for internal context
DEFAULT_SLACK_CHANNELS = "sales,marketing"
SLACK_CHANNELS = [
    channel.strip() 
    for channel in os.getenv("SLACK_CHANNELS", DEFAULT_SLACK_CHANNELS).split(",") 
    if channel.strip()
]

# Fixed assignee routing
ASSIGNEES = [
    {"id": "ritu", "email": "ritu@adopt.ai"},
    {"id": "sravan", "email": "sravan@adopt.ai"},
]
LEADS_PER_ASSIGNEE = int(os.getenv("LEADS_PER_ASSIGNEE", "10"))

# Number of top leads to include in digest
TOP_LEADS_COUNT = int(os.getenv("TOP_LEADS_COUNT", str(len(ASSIGNEES) * LEADS_PER_ASSIGNEE)))

# Optional web search (Parallel.ai Search API)
PARALLEL_SEARCH_URL = "https://api.parallel.ai/v1beta/search"
PARALLEL_SEARCH_HEADERS = {"parallel-beta": "search-extract-2025-10-10"}

# Engagement scoring method: "hubspot" (use HubSpot lead_scoring_total) or "custom" (calculate custom score)
USE_HUBSPOT_SCORING = os.getenv("USE_HUBSPOT_SCORING", "true").lower() == "true"

# Path to the processed contacts log file
PROCESSED_CONTACTS_LOG = os.path.join(os.path.dirname(__file__), "processed_contacts_log.json")

# Lead scoring threshold priority order (highest to lowest)
LEAD_SCORING_PRIORITY = {
    "A1": 1,
    "A2": 2,
    "B1": 3,
    "A3": 4,
    "B2": 5,
    "C1": 6,
    "B3": 7,
    "C2": 8,
    "C3": 9
}

def get_lead_scoring_priority(threshold: str) -> int:
    """Get priority value for lead scoring threshold.
    
    Lower number = higher priority.
    Returns 999 if threshold is not in priority list.
    """
    return LEAD_SCORING_PRIORITY.get(threshold.upper(), 999)


class HubSpotLeadClient:
    """Client for HubSpot API interactions focused on lead/contact operations."""
    
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
    
    def get_all_contacts(self, properties: list[str], limit: int = 100) -> list[dict]:
        """Fetch all contacts with specified properties."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts"
        
        all_contacts = []
        after = None
        
        while True:
            params = {
                "limit": limit,
                "properties": ",".join(properties)
            }
            if after:
                params["after"] = after
            
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            all_contacts.extend(data.get("results", []))
            
            paging = data.get("paging", {})
            if paging.get("next"):
                after = paging["next"]["after"]
            else:
                break
        
        return all_contacts
    
    def search_contacts(self, filters: list[dict], properties: list[str]) -> list[dict]:
        """Search for contacts with specific filters."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/search"
        
        payload = {
            "filterGroups": [{"filters": filters}] if filters else [],
            "properties": properties,
            "limit": 100
        }
        
        all_contacts = []
        after = None
        
        while True:
            if after:
                payload["after"] = after
            
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()
            
            all_contacts.extend(data.get("results", []))
            
            paging = data.get("paging", {})
            if paging.get("next"):
                after = paging["next"]["after"]
            else:
                break
        
        return all_contacts
    
    def get_contact_deal_associations(self, contact_id: str) -> list[dict]:
        """Check if a contact has any associated deals."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/contacts/{contact_id}/associations/deals"
        
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        
        return response.json().get("results", [])
    
    def get_contact_meeting_associations(self, contact_id: str) -> list[dict]:
        """Get meetings associated with a contact."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/contacts/{contact_id}/associations/meetings"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json().get("results", [])
        except requests.exceptions.RequestException:
            return []
    
    def get_associated_company(self, contact_id: str) -> Optional[dict]:
        """Get the company associated with a contact."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/contacts/{contact_id}/associations/companies"
        
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
            params={"properties": "name,industry,numberofemployees,description,website,country,city,annualrevenue"}
        )
        company_response.raise_for_status()
        
        return company_response.json()
    
    def get_contact_emails(self, contact_id: str, limit: int = 20) -> list[dict]:
        """Get emails associated with a contact."""
        all_email_ids = []
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}/associations/emails"
        
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
        
        # Fetch email details
        return self._fetch_emails_by_ids(all_email_ids[:limit])
    
    def _fetch_emails_by_ids(self, email_ids: list[str]) -> list[dict]:
        """Fetch email details by IDs."""
        if not email_ids:
            return []
        
        unique_ids = list(set(email_ids))
        all_emails = []
        emails_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/emails/batch/read"
        
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
    
    def get_contact_notes(self, contact_id: str, limit: int = 5) -> list[dict]:
        """Get notes associated with a contact."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}/associations/notes"
        
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


class ApolloClient:
    """Client for Apollo.io API interactions for contact/company enrichment."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.apollo.io/api/v1"
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": api_key
        }
    
    def enrich_contact(self, email: str) -> dict:
        """Enrich a contact by email address.
        
        Returns company info, contact details, intent signals, and similar companies.
        """
        url = f"{self.base_url}/people/match"
        
        # Apollo API takes parameters as query params
        params = {
            "email": email,
            "reveal_personal_emails": "false"
        }
        
        try:
            response = requests.post(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            person = data.get("person", {})
            organization = person.get("organization", {})
            
            return {
                "found": bool(person),
                "contact": {
                    "name": person.get("name", ""),
                    "title": person.get("title", ""),
                    "seniority": person.get("seniority", ""),
                    "department": person.get("departments", []),
                    "linkedin_url": person.get("linkedin_url", ""),
                },
                "company": {
                    "name": organization.get("name", ""),
                    "website": organization.get("website_url", ""),
                    "industry": organization.get("industry", ""),
                    "employee_count": organization.get("estimated_num_employees", 0),
                    "funding_stage": organization.get("funding_stage", ""),
                    "total_funding": organization.get("total_funding", 0),
                    "latest_funding_round": organization.get("latest_funding_round_type", ""),
                    "tech_stack": organization.get("technologies", []),
                    "keywords": organization.get("keywords", []),
                    "city": organization.get("city", ""),
                    "country": organization.get("country", ""),
                    "linkedin_url": organization.get("linkedin_url", ""),
                    "phone": organization.get("phone", ""),
                    "annual_revenue": organization.get("annual_revenue", ""),
                },
                "intent_signals": {
                    "hiring_signal": bool(organization.get("current_job_openings", [])),
                    "job_openings": organization.get("current_job_openings", [])[:5],
                    "recent_news": person.get("employment_history", [])[:2],
                }
            }
            
        except requests.exceptions.RequestException as e:
            print(f"   ⚠️ Apollo API error: {e}")
            return {"found": False, "error": str(e)}
    
    def format_apollo_context(self, enrichment: dict) -> str:
        """Format Apollo enrichment data into a readable context string."""
        if not enrichment.get("found"):
            return "No Apollo enrichment data available."
        
        company = enrichment.get("company", {})
        contact = enrichment.get("contact", {})
        intent = enrichment.get("intent_signals", {})
        
        lines = []
        
        # Company info
        if company.get("name"):
            lines.append(f"**Company**: {company['name']}")
            if company.get("industry"):
                lines.append(f"  - Industry: {company['industry']}")
            if company.get("employee_count"):
                lines.append(f"  - Employees: {company['employee_count']:,}")
            if company.get("funding_stage"):
                lines.append(f"  - Funding Stage: {company['funding_stage']}")
            if company.get("total_funding"):
                lines.append(f"  - Total Funding: ${company['total_funding']:,}")
            if company.get("annual_revenue"):
                lines.append(f"  - Annual Revenue: {company['annual_revenue']}")
            if company.get("tech_stack"):
                tech_list = company['tech_stack'][:10]
                lines.append(f"  - Tech Stack: {', '.join(tech_list)}")
        
        # Contact info
        if contact.get("seniority"):
            lines.append(f"**Contact Seniority**: {contact['seniority']}")
        if contact.get("department"):
            lines.append(f"**Department**: {', '.join(contact['department'])}")
        
        # Intent signals
        if intent.get("hiring_signal"):
            lines.append(f"**Hiring Signal**: Currently hiring")
            if intent.get("job_openings"):
                openings = [j.get("title", "") for j in intent["job_openings"] if j.get("title")]
                if openings:
                    lines.append(f"  - Open roles: {', '.join(openings[:3])}")
        
        return "\n".join(lines) if lines else "Limited Apollo data available."


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
        """Search for messages in specified channels."""
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
        
        formatted = []
        for msg in messages[:limit]:
            formatted.append({
                "text": msg.get("text", "")[:500],
                "user": msg.get("username", msg.get("user", "Unknown")),
                "channel": msg.get("channel", {}).get("name", "unknown"),
                "timestamp": msg.get("ts", ""),
                "permalink": msg.get("permalink", "")
            })
        
        return formatted
    
    def format_slack_context(self, messages: list[dict]) -> str:
        """Format Slack messages into a readable context string."""
        if not messages:
            return "No relevant Slack discussions found."
        
        formatted_messages = []
        for msg in messages:
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
        """Search for meeting transcripts by title."""
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
    
    def format_fireflies_context(self, transcripts: list[dict]) -> str:
        """Format Fireflies transcripts into a readable context string."""
        if not transcripts:
            return "No call transcripts found for this contact."
        
        formatted_transcripts = []
        
        for transcript in transcripts:
            date_val = transcript.get("date", "Unknown date")
            date_str = "Unknown date"
            if date_val:
                try:
                    if isinstance(date_val, (int, float)):
                        ts = date_val / 1000 if date_val > 1e12 else date_val
                        dt = datetime.fromtimestamp(ts)
                        date_str = dt.strftime("%Y-%m-%d")
                    elif isinstance(date_val, str):
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
        """Search the knowledge base for relevant content.
        
        Args:
            query: The search query (e.g., persona, industry, use case)
            n_results: Number of results to return
            
        Returns:
            List of dicts with 'content', 'source', and 'category' keys
        """
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
    
    def get_context_for_lead(self, lead_context: dict) -> str:
        """Build a comprehensive knowledge base context for a specific lead.
        
        Searches for:
        1. Industry-specific use cases and capabilities
        2. Persona-relevant messaging (based on job title)
        3. Company size-appropriate value propositions
        """
        if not self._initialized and not self.initialize():
            return "Knowledge base not available."
        
        all_results = []
        
        # Search by industry
        industry = lead_context.get("company_industry", "")
        if industry and industry != "Unknown":
            results = self.search(f"{industry} industry use case capabilities", n_results=3)
            all_results.extend(results)
        
        # Search by job title/persona
        job_title = lead_context.get("contact_title", "")
        if job_title and job_title != "Unknown":
            results = self.search(f"{job_title} persona messaging value proposition", n_results=3)
            all_results.extend(results)
        
        # Search by company context (tech stack, etc.)
        apollo_data = lead_context.get("apollo_enrichment", {})
        if apollo_data.get("found"):
            company_data = apollo_data.get("company", {})
            tech_stack = company_data.get("tech_stack", [])
            if tech_stack:
                tech_query = " ".join(tech_stack[:5])
                results = self.search(f"integration {tech_query} API", n_results=2)
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
            content = r.get("content", "")[:800]  # Limit content length
            
            formatted.append(f"[{category}/{source}]\n{content}")
        
        return "\n\n---\n\n".join(formatted)


def load_processed_contacts_log() -> dict:
    """Load the processed contacts log file.
    
    Processed contacts are only added after a run completes (digest built and sent). Each run
    adds the TOP_LEADS_COUNT leads it processed (top N by priority; order in the file is
    arbitrary because we store sets). Failed runs do not add to the log.
    
    Returns:
        Dictionary with 'processed_contact_ids' (set of contact IDs) and 'processed_emails' (set of emails)
    """
    if not os.path.exists(PROCESSED_CONTACTS_LOG):
        return {
            "processed_contact_ids": set(),
            "processed_emails": set(),
            "last_updated": None
        }
    
    try:
        with open(PROCESSED_CONTACTS_LOG, "r") as f:
            data = json.load(f)
            return {
                "processed_contact_ids": set(data.get("processed_contact_ids", [])),
                "processed_emails": set(data.get("processed_emails", [])),
                "last_updated": data.get("last_updated")
            }
    except (json.JSONDecodeError, IOError) as e:
        print(f"   ⚠️ Error loading processed contacts log: {e}")
        return {
            "processed_contact_ids": set(),
            "processed_emails": set(),
            "last_updated": None
        }


def save_processed_contacts_log(processed_contact_ids: set, processed_emails: set):
    """Save the processed contacts log file.
    
    Args:
        processed_contact_ids: Set of contact IDs that have been processed
        processed_emails: Set of email addresses that have been processed
    """
    log_data = {
        "processed_contact_ids": list(processed_contact_ids),
        "processed_emails": list(processed_emails),
        "last_updated": datetime.now().isoformat()
    }
    
    try:
        with open(PROCESSED_CONTACTS_LOG, "w") as f:
            json.dump(log_data, f, indent=2)
        print(f"   📝 Updated processed contacts log: {len(processed_contact_ids)} contacts")
    except IOError as e:
        print(f"   ⚠️ Error saving processed contacts log: {e}")


def is_contact_already_processed(contact: dict, processed_log: dict) -> bool:
    """Check if a contact has already been processed.
    
    Args:
        contact: Contact dictionary from HubSpot
        processed_log: Dictionary from load_processed_contacts_log()
    
    Returns:
        True if contact has been processed, False otherwise
    """
    contact_id = contact.get("id")
    contact_email = contact.get("properties", {}).get("email", "").lower().strip()
    
    if contact_id and contact_id in processed_log["processed_contact_ids"]:
        return True
    
    if contact_email and contact_email in processed_log["processed_emails"]:
        return True
    
    return False


def get_engagement_score(contact: dict, meeting_count: int = 0) -> int:
    """Get engagement score for a contact.
    
    If USE_HUBSPOT_SCORING is True, uses HubSpot's lead_scoring_engagement property only.
    Otherwise, calculates a custom engagement score.
    
    Args:
        contact: Contact dictionary from HubSpot
        meeting_count: Number of meetings associated with the contact
    
    Returns:
        Engagement score as integer (0 if HubSpot scoring is enabled but not available)
    """
    props = contact.get("properties", {})
    
    # Use HubSpot scoring if enabled (no fallback to custom)
    if USE_HUBSPOT_SCORING:
        hubspot_score = props.get("lead_scoring_engagement")
        if hubspot_score:
            try:
                # Handle both string and numeric values
                return int(float(str(hubspot_score)))
            except (ValueError, TypeError):
                return 0  # Return 0 if conversion fails (don't use custom scoring)
        return 0  # Return 0 if HubSpot score not available (don't use custom scoring)
    
    # Use custom scoring only if HubSpot scoring is disabled
    return calculate_custom_engagement_score(contact, meeting_count)


def calculate_custom_engagement_score(contact: dict, meeting_count: int = 0) -> int:
    """Calculate custom engagement score for a contact based on HubSpot activity properties.
    
    This is used when HubSpot scoring is not available or USE_HUBSPOT_SCORING is False.
    
    Scoring:
    - Email opens: 2 points per open (max 20)
    - Email clicks: 5 points per click (max 25)
    - Email replies: 15 points if recent reply exists
    - Website visits: 1 point per page view (max 15)
    - Form submissions: 10 points per conversion (max 30)
    - Meetings: 20 points per meeting (max 40)
    
    Total possible: ~145 points
    
    Args:
        contact: Contact dictionary from HubSpot
        meeting_count: Number of meetings associated with the contact
    
    Returns:
        Custom engagement score as integer
    """
    props = contact.get("properties", {})
    score = 0
    
    # Email opens (2 pts each, max 20)
    email_opens = int(props.get("hs_email_open_count", 0) or 0)
    score += min(email_opens * 2, 20)
    
    # Email clicks (5 pts each, max 25)
    email_clicks = int(props.get("hs_email_click_count", 0) or 0)
    score += min(email_clicks * 5, 25)
    
    # Email replies (15 pts if recent)
    last_reply = props.get("hs_sales_email_last_replied")
    if last_reply:
        score += 15
    
    # Website visits (1 pt each, max 15)
    page_views = int(props.get("hs_analytics_num_page_views", 0) or 0)
    score += min(page_views, 15)
    
    # Form submissions (10 pts each, max 30)
    conversions = int(props.get("num_conversion_events", 0) or 0)
    score += min(conversions * 10, 30)
    
    # Meetings (20 pts each, max 40)
    score += min(meeting_count * 20, 40)
    
    return score


def is_contact_stale(contact: dict, threshold_days: int = STALE_THRESHOLD_DAYS) -> bool:
    """Check if a contact is stale based on last activity date."""
    props = contact.get("properties", {})
    
    # Check multiple last-contacted properties
    last_contacted = props.get("notes_last_contacted")
    last_email_sent = props.get("hs_sales_email_last_sent")
    last_activity = props.get("hs_last_sales_activity_timestamp")
    
    # Find the most recent activity date
    most_recent = None
    
    for date_val in [last_contacted, last_email_sent, last_activity]:
        if not date_val:
            continue
        try:
            if isinstance(date_val, str):
                dt = datetime.fromisoformat(date_val.replace("Z", "+00:00"))
            else:
                dt = datetime.fromtimestamp(date_val / 1000)
            
            if most_recent is None or dt > most_recent:
                most_recent = dt
        except (ValueError, TypeError):
            continue
    
    # If no activity found, consider it stale
    if most_recent is None:
        return True
    
    # Check if activity is older than threshold
    now = datetime.now(most_recent.tzinfo) if most_recent.tzinfo else datetime.now()
    cutoff = now - timedelta(days=threshold_days)
    
    return most_recent < cutoff


def passes_filters(contact: dict, company: Optional[dict]) -> tuple[bool, str]:
    """Check if a contact passes all configured filters.
    
    All filters are optional except lead_scoring_threshold which must exist when USE_HUBSPOT_SCORING is True.
    
    Returns (passes, reason) tuple.
    """
    props = contact.get("properties", {})
    company_props = company.get("properties", {}) if company else {}
    
    # Required filter: lead_scoring_threshold must exist (only if USE_HUBSPOT_SCORING is true)
    if USE_HUBSPOT_SCORING:
        lead_scoring_threshold = props.get("lead_scoring_threshold", "")
        if not lead_scoring_threshold:
            return False, "No lead_scoring_threshold property (required when USE_HUBSPOT_SCORING=true)"
    
    # Optional filter: Employee size
    if MIN_EMPLOYEE_SIZE > 0:
        employee_count = company_props.get("numberofemployees")
        if employee_count:
            try:
                emp_count = int(employee_count)
                if emp_count < MIN_EMPLOYEE_SIZE:
                    return False, f"Company size ({emp_count}) below minimum ({MIN_EMPLOYEE_SIZE})"
            except (ValueError, TypeError):
                pass
    
    # Optional filter: Industry
    if TARGET_INDUSTRIES:
        company_industry = company_props.get("industry", "")
        if company_industry and company_industry not in TARGET_INDUSTRIES:
            return False, f"Industry '{company_industry}' not in target list"
    
    # Optional filter: Country
    if TARGET_COUNTRIES:
        contact_country = props.get("country", "")
        company_country = company_props.get("country", "")
        country = contact_country or company_country
        if country and country not in TARGET_COUNTRIES:
            return False, f"Country '{country}' not in target list"
    
    # Optional filter: Job title keywords
    if TARGET_JOB_TITLES:
        job_title = props.get("jobtitle", "")
        if job_title:
            title_lower = job_title.lower()
            if not any(keyword.lower() in title_lower for keyword in TARGET_JOB_TITLES):
                return False, f"Job title '{job_title}' doesn't match target keywords"
    
    # Optional filter: Lifecycle stage
    if TARGET_LIFECYCLE_STAGES:
        lifecycle_stage = props.get("lifecyclestage", "").lower()
        if lifecycle_stage and lifecycle_stage not in TARGET_LIFECYCLE_STAGES:
            return False, f"Lifecycle stage '{lifecycle_stage}' not in target list"
    
    return True, "Passed all filters"


def format_previous_emails_context(emails: list[dict], max_body_chars: int = 400) -> str:
    """Format HubSpot email objects into a readable context string (newest first).
    
    Each email has properties: hs_email_subject, hs_email_direction, hs_timestamp,
    hs_email_text, hs_createdate.
    """
    if not emails:
        return "No previous emails found for this contact."
    
    # Sort by timestamp descending (most recent first). HubSpot timestamps are often in ms.
    def _ts(e: dict) -> float:
        props = e.get("properties", {})
        ts = props.get("hs_timestamp") or props.get("hs_createdate")
        if ts is None:
            return 0.0
        try:
            t = float(ts)
            return t if t > 1e12 else t * 1000  # treat as ms
        except (TypeError, ValueError):
            return 0.0
    
    sorted_emails = sorted(emails, key=_ts, reverse=True)
    lines = []
    for e in sorted_emails[:15]:  # cap at 15 most recent
        props = e.get("properties", {})
        direction = (props.get("hs_email_direction") or "UNKNOWN").replace("_", " ").title()
        subject = (props.get("hs_email_subject") or "(No subject)").strip()
        body = (props.get("hs_email_text") or "").strip()
        if body and len(body) > max_body_chars:
            body = body[:max_body_chars] + "..."
        ts = props.get("hs_timestamp") or props.get("hs_createdate")
        date_str = "Unknown date"
        if ts:
            try:
                t = float(ts)
                if t > 1e12:
                    t = t / 1000
                date_str = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                date_str = str(ts)
        lines.append(f"[{date_str}] {direction}\nSubject: {subject}\n{body or '(No body)'}")
        lines.append("")  # blank line between emails
    
    return "\n".join(lines).strip()


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


def generate_outreach_email(client: anthropic.Anthropic, lead_context: dict) -> dict:
    """Use Claude to generate a personalized outreach email for a lead."""
    
    prompt = f"""## Role & Purpose

You are an AI sales assistant for Adopt AI, specializing in generating personalized outreach emails for high-engagement leads who haven't been contacted recently. Your goal is to re-engage prospects by connecting their demonstrated interest (engagement signals) to relevant value propositions.

## Lead Context

**Contact:**
- Name: {lead_context['contact_name']}
- Title: {lead_context['contact_title']}
- Email: {lead_context['contact_email']}

**Company:**
- Name: {lead_context['company_name']}
- Industry: {lead_context['company_industry']}
- Size: {lead_context['company_size']} employees

**Engagement Signals:**
- Engagement Score: {lead_context['engagement_score']}/100
- Email Opens: {lead_context.get('email_opens', 0)}
- Email Clicks: {lead_context.get('email_clicks', 0)}
- Website Page Views: {lead_context.get('page_views', 0)}
- Form Submissions: {lead_context.get('form_submissions', 0)}
- Days Since Last Activity: {lead_context['days_since_activity']}

**Apollo Enrichment Data:**
{lead_context.get('apollo_context', 'No Apollo data available.')}

**Internal Slack Discussions:**
{lead_context.get('slack_context', 'No Slack context available.')}

**Call Recording Transcripts (from Fireflies):**
{lead_context.get('fireflies_context', 'No call transcripts available.')}

**Recent Company News & Intelligence (from web search):**
{lead_context.get('web_research', 'No web research available.')}

**Recent Notes:**
{lead_context.get('notes', 'No notes available.')}

**Previous Emails (from HubSpot):**
Use this thread history to avoid repeating ourselves and to reference what was already discussed. If this is a re-engagement, acknowledge prior contact and build on it.
{lead_context.get('previous_emails_context', 'No previous emails found for this contact.')}

## Adopt AI Knowledge Base (Relevant Context)

The following content was retrieved from our knowledge base based on this lead's industry, persona, and company profile. Use this to craft a more personalized and relevant email:

{lead_context.get('knowledge_base_context', 'No knowledge base content available.')}

## Your Task

### Step 1: Analyze the Context

From the engagement signals, Apollo data, Slack discussions, call transcripts, web research, and previous emails, identify:
1. **Interest Indicators**: What has this lead engaged with? What are they interested in?
2. **Company Context**: What does Apollo tell us about their company, tech stack, or hiring signals?
3. **Internal Intelligence**: What do we know from Slack or previous calls?
4. **Email Thread**: If there are previous emails, what was said? What should we reference or avoid repeating? Is this a follow-up or net-new angle?
5. **Relevant Angle**: What capability or use case would resonate most?
6. **Lead Source**: Where did this lead find Adopt AI? (LinkedIn, website, conference, etc.)

### Step 2: Generate the Email

**Email Structure (80-120 words MAX):**
1. **Subject Line**: Reference their engagement, a specific interest, or the previous thread (e.g. "Re: ..." if following up).
2. **Opening**: Acknowledge their engagement or prior conversation naturally. If they replied before, reference it; if not, "I noticed you've been exploring..." style is fine.
3. **Value Hook**: Connect their apparent interest (or what they said in prior emails) to a specific capability or outcome.
4. **Social Proof (optional)**: Brief mention of similar company or use case.
5. **Simple CTA**: One clear, low-friction ask. Do not repeat CTAs or offers already made in previous emails.

**Tone Guidelines:**
- Professional but warm
- Show you've noticed their specific engagement
- Focus on THEIR potential use case, not our features
- Concise and respectful of their time
- Use Poke the bear style of little bit aggressive sales language
- Don't use long sentences, use short sentences and paragraphs.

## Response Format

Respond with JSON in this exact format:
{{
    "analysis": {{
        "engagement_summary": "Brief summary of their engagement patterns",
        "company_insights": "Key insights from Apollo enrichment",
        "recommended_angle": "The approach you're taking and why"
    }},
    "subject": "Email subject line",
    "body": "The email body (80-120 words max)",
    "flags": ["Any missing info or recommendations"]
}}
"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    response_text = response.content[0].text
    
    try:
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text.strip()
        
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {
            "analysis": {
                "engagement_summary": "Unable to parse - see raw response",
                "company_insights": "Unknown",
                "recommended_angle": "Unknown"
            },
            "subject": "Let's connect",
            "body": response_text,
            "flags": ["Failed to parse structured response"]
        }


def generate_linkedin_connection_note(client: anthropic.Anthropic, lead_context: dict) -> str:
    """Generate a short LinkedIn connection request note (max 300 characters) for a lead."""

    prompt = f"""## Role & Purpose

You are an AI sales assistant for Adopt AI. Generate a single LinkedIn connection note that will be sent when we send this person a connection request. LinkedIn limits connection notes to 300 characters.

## Lead Context

**Contact:** {lead_context['contact_name']} – {lead_context['contact_title']} at {lead_context['company_name']}
**Company:** {lead_context['company_industry']}, {lead_context['company_size']} employees

**Brief context (use for personalization):**
- Apollo: {lead_context.get('apollo_context', 'No Apollo data')[:400]}
- Recommended angle from our analysis: {lead_context.get('analysis', {}).get('recommended_angle', 'N/A') if isinstance(lead_context.get('analysis'), dict) else 'N/A'}

## Requirements

- Maximum 300 characters (LinkedIn hard limit). Count carefully.
- Personal and relevant to their role/company – reference something specific.
- No hard sell. Goal is to get them to accept the connection.
- One or two short sentences. Natural, conversational tone.
- Do not include greetings like "Hi [Name]" if it wastes space; we can add that when sending. Optional: you may start with first name only.

## Response Format

Respond with JSON only:
{{"note": "Your connection note here, under 300 characters."}}
"""

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = response.content[0].text.strip()
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text
        data = json.loads(json_str)
        note = (data.get("note") or response_text or "").strip()
        return note[:300]
    except Exception:
        return ""


def format_lead_digest_html(ritu_leads: list[dict], sravan_leads: list[dict]) -> str:
    """Format leads into an HTML digest email with two sections (one for each person)."""
    
    date_str = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    total_count = len(ritu_leads) + len(sravan_leads)
    
    def format_lead_card(lead: dict, index: int) -> str:
        """Format a single lead card."""
        # Build Apollo insights HTML
        apollo_html = ""
        apollo_data = lead.get("apollo_enrichment", {})
        if apollo_data.get("found"):
            company_data = apollo_data.get("company", {})
            contact_data = apollo_data.get("contact", {})
            intent_data = apollo_data.get("intent_signals", {})
            
            apollo_items = []
            if company_data.get("funding_stage"):
                apollo_items.append(f"<div class='apollo-item'><strong>Funding:</strong> {company_data['funding_stage']}</div>")
            if company_data.get("tech_stack"):
                tech_preview = ", ".join(company_data["tech_stack"][:5])
                apollo_items.append(f"<div class='apollo-item'><strong>Tech Stack:</strong> {tech_preview}</div>")
            if contact_data.get("seniority"):
                apollo_items.append(f"<div class='apollo-item'><strong>Seniority:</strong> {contact_data['seniority']}</div>")
            if intent_data.get("hiring_signal"):
                apollo_items.append(f"<div class='apollo-item'><strong>🔥 Hiring Signal:</strong> Currently hiring</div>")
            
            if apollo_items:
                apollo_html = f"""
                <div class="apollo-insights">
                    <h4>🔍 Apollo Insights</h4>
                    {"".join(apollo_items)}
                </div>
"""
        
        # Build analysis HTML
        analysis_html = ""
        analysis = lead.get("analysis", {})
        if analysis:
            analysis_html = f"""
                <div class="analysis">
                    <h4>🧠 AI Analysis</h4>
                    <div class="analysis-item"><strong>Engagement:</strong> {analysis.get('engagement_summary', 'N/A')}</div>
                    <div class="analysis-item"><strong>Company Insights:</strong> {analysis.get('company_insights', 'N/A')}</div>
                    <div class="analysis-item"><strong>Recommended Angle:</strong> {analysis.get('recommended_angle', 'N/A')}</div>
                </div>
"""
        
        # Build flags HTML
        flags_html = ""
        flags = lead.get("flags", [])
        if flags:
            flag_items = "".join(f"<li>{f}</li>" for f in flags)
            flags_html = f"""
                <div class="flags">
                    <h4>⚠️ Flags</h4>
                    <ul>{flag_items}</ul>
                </div>
"""
        
        # Build lead scoring HTML
        lead_scoring_html = ""
        threshold = lead.get("lead_scoring_threshold", "N/A")
        total_score = lead.get("lead_scoring_total", "N/A")
        fit_score = lead.get("lead_scoring_fit", "N/A")
        engagement_score = lead.get("lead_scoring_engagement", "N/A")
        
        # Determine threshold color based on priority
        threshold_colors = {
            "A1": "#d32f2f",  # Red - hottest
            "A2": "#f57c00",  # Orange
            "B1": "#f57c00",  # Orange
            "A3": "#fbc02d",  # Yellow
            "B2": "#689f38",  # Light green
            "C1": "#1976d2",  # Blue
            "B3": "#616161",  # Grey
            "C2": "#616161",  # Grey
            "C3": "#616161",  # Grey
        }
        threshold_color = threshold_colors.get(threshold, "#616161")
        
        lead_scoring_html = f"""
                <div class="lead-scoring" style="background: #f3e5f5; border-radius: 8px; padding: 15px; margin: 15px 0; border-left: 4px solid {threshold_color};">
                    <h4 style="margin: 0 0 10px 0; color: {threshold_color}; font-size: 13px; text-transform: uppercase;">🎯 Lead Scoring</h4>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px;">
                        <div style="background: white; padding: 10px; border-radius: 6px;">
                            <div style="font-size: 11px; color: #666; margin-bottom: 5px;">Threshold</div>
                            <div style="font-size: 18px; font-weight: bold; color: {threshold_color};">{threshold}</div>
                        </div>
                        <div style="background: white; padding: 10px; border-radius: 6px;">
                            <div style="font-size: 11px; color: #666; margin-bottom: 5px;">Total Score</div>
                            <div style="font-size: 18px; font-weight: bold; color: #333;">{total_score}</div>
                        </div>
                        <div style="background: white; padding: 10px; border-radius: 6px;">
                            <div style="font-size: 11px; color: #666; margin-bottom: 5px;">Fit Score</div>
                            <div style="font-size: 18px; font-weight: bold; color: #333;">{fit_score}</div>
                        </div>
                        <div style="background: white; padding: 10px; border-radius: 6px;">
                            <div style="font-size: 11px; color: #666; margin-bottom: 5px;">Engagement Score</div>
                            <div style="font-size: 18px; font-weight: bold; color: #333;">{engagement_score}</div>
                        </div>
                    </div>
                </div>
"""
        
        return f"""
    <div class="lead-card">
        <div class="lead-header">
            <span class="lead-name">#{index} {lead['contact_name']}</span>
            <span class="engagement-score">Score: {lead['engagement_score']}</span>
        </div>
        <div class="lead-meta">
            <strong>{lead['contact_title']}</strong> at <strong>{lead['company_name']}</strong><br>
            {lead['contact_email']} • {lead['company_industry']} • {lead['company_size']} employees<br>
            Last activity: {lead['days_since_activity']} days ago{f" • <a href='{lead['contact_linkedin_url']}' style='color: #0077b5;'>LinkedIn</a>" if lead.get('contact_linkedin_url') else ""}
        </div>
        {lead_scoring_html}
        {apollo_html}
        {analysis_html}
        {flags_html}
        <div class="email-preview">
            <div class="email-subject">📝 Subject: {html_module.escape(lead['email_subject'])}</div>
            <div class="email-body">{html_module.escape(lead['email_body'])}</div>
        </div>
        {f'<div class="linkedin-note"><h4>🔗 LinkedIn connection note</h4><p>{html_module.escape(lead.get("linkedin_connection_note", ""))}</p><span class="linkedin-note-hint">Use when sending connection request (max 300 chars)</span></div>' if lead.get("linkedin_connection_note") else ""}
    </div>
"""
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 900px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 30px; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .header p {{ margin: 10px 0 0 0; opacity: 0.9; }}
        .section-header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; margin: 30px 0 20px 0; }}
        .section-header h2 {{ margin: 0; font-size: 20px; }}
        .section-header p {{ margin: 10px 0 0 0; opacity: 0.9; font-size: 14px; }}
        .lead-card {{ background: #f8f9fa; border-radius: 10px; padding: 25px; margin-bottom: 25px; border-left: 4px solid #11998e; }}
        .lead-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; flex-wrap: wrap; gap: 10px; }}
        .lead-name {{ font-size: 18px; font-weight: 600; color: #333; }}
        .engagement-score {{ background: #11998e; color: white; padding: 4px 12px; border-radius: 20px; font-size: 12px; }}
        .lead-meta {{ color: #666; font-size: 14px; margin-bottom: 15px; }}
        .apollo-insights {{ background: #e3f2fd; border-radius: 8px; padding: 15px; margin: 15px 0; border-left: 3px solid #1976d2; }}
        .apollo-insights h4 {{ margin: 0 0 10px 0; color: #1976d2; font-size: 13px; text-transform: uppercase; }}
        .apollo-item {{ font-size: 13px; color: #555; margin-bottom: 5px; }}
        .email-preview {{ background: white; border-radius: 8px; padding: 20px; margin-top: 15px; }}
        .email-subject {{ font-weight: 600; color: #333; margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; }}
        .email-body {{ white-space: pre-wrap; color: #444; }}
        .linkedin-note {{ background: #e8f4f8; border-radius: 8px; padding: 15px; margin-top: 15px; border-left: 3px solid #0077b5; }}
        .linkedin-note h4 {{ margin: 0 0 8px 0; color: #0077b5; font-size: 13px; text-transform: uppercase; }}
        .linkedin-note p {{ margin: 0; color: #333; font-size: 14px; white-space: pre-wrap; }}
        .linkedin-note-hint {{ display: block; font-size: 11px; color: #666; margin-top: 8px; }}
        .analysis {{ background: #fff3e0; border-radius: 8px; padding: 15px; margin: 15px 0; border-left: 3px solid #ff9800; }}
        .analysis h4 {{ margin: 0 0 10px 0; color: #e65100; font-size: 13px; text-transform: uppercase; }}
        .analysis-item {{ font-size: 13px; color: #555; margin-bottom: 8px; }}
        .flags {{ background: #fff3cd; border-radius: 8px; padding: 15px; margin: 15px 0; border-left: 3px solid #ffc107; }}
        .flags h4 {{ margin: 0 0 10px 0; color: #856404; font-size: 13px; text-transform: uppercase; }}
        .flags ul {{ margin: 0; padding-left: 20px; }}
        .flags li {{ color: #856404; margin-bottom: 5px; font-size: 13px; }}
        .footer {{ text-align: center; color: #999; font-size: 12px; margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; }}
        .no-leads {{ text-align: center; padding: 40px; color: #666; }}
        .stats {{ display: flex; gap: 20px; margin-top: 15px; }}
        .stat {{ background: rgba(255,255,255,0.2); padding: 10px 15px; border-radius: 8px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; }}
        .stat-label {{ font-size: 12px; opacity: 0.9; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🎯 Lead Finder Digest</h1>
        <p>High-Engagement Stale Leads - Generated on {date_str}</p>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{total_count}</div>
                <div class="stat-label">Total Leads</div>
            </div>
            <div class="stat">
                <div class="stat-value">{len(ritu_leads)}</div>
                <div class="stat-label">Ritu's Leads</div>
            </div>
            <div class="stat">
                <div class="stat-value">{len(sravan_leads)}</div>
                <div class="stat-label">Sravan's Leads</div>
            </div>
        </div>
    </div>
"""
    
    # Ritu's section
    html += f"""
    <div class="section-header">
        <h2>👤 Leads for Ritu (ritu@adopt.ai)</h2>
        <p>{len(ritu_leads)} leads assigned</p>
    </div>
"""
    
    if not ritu_leads:
        html += """
    <div class="no-leads">
        <p>No leads assigned to Ritu.</p>
    </div>
"""
    else:
        for i, lead in enumerate(ritu_leads, 1):
            html += format_lead_card(lead, i)
    
    # Sravan's section
    html += f"""
    <div class="section-header">
        <h2>👤 Leads for Sravan (sravan@adopt.ai)</h2>
        <p>{len(sravan_leads)} leads assigned</p>
    </div>
"""
    
    if not sravan_leads:
        html += """
    <div class="no-leads">
        <p>No leads assigned to Sravan.</p>
    </div>
"""
    else:
        for i, lead in enumerate(sravan_leads, 1):
            html += format_lead_card(lead, i)
    
    html += """
    <div class="footer">
        <p>This digest was automatically generated by the Lead Finder AI Agent.<br>
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
        "from": {"email": FROM_EMAIL, "name": "Lead Finder Agent"},
        "subject": f"🎯 Lead Finder Digest - {datetime.now().strftime('%B %d, %Y')}",
        "content": [{"type": "text/html", "value": html_content}]
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    
    print(f"✅ Digest email sent to {', '.join(to_emails)}")


def main():
    """Main execution flow."""
    print("🎯 Starting Lead Finder Agent...")
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
    
    hubspot = HubSpotLeadClient(hubspot_token)
    claude = anthropic.Anthropic(api_key=anthropic_key)
    
    # Initialize optional clients
    apollo_key = os.getenv("APOLLO_API_KEY")
    apollo = ApolloClient(apollo_key) if apollo_key else None
    if apollo:
        print("🔗 Apollo integration enabled")
    else:
        print("ℹ️ Apollo integration disabled (APOLLO_API_KEY not set)")
    
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    slack = SlackClient(slack_token) if slack_token else None
    if slack:
        print(f"💬 Slack integration enabled (searching: #{', #'.join(SLACK_CHANNELS)})")
    else:
        print("ℹ️ Slack integration disabled (SLACK_BOT_TOKEN not set)")
    
    fireflies_key = os.getenv("FIREFLIES_API_KEY")
    fireflies = FirefliesClient(fireflies_key) if fireflies_key else None
    if fireflies:
        print("📞 Fireflies integration enabled")
    else:
        print("ℹ️ Fireflies integration disabled (FIREFLIES_API_KEY not set)")
    
    # Initialize knowledge base client
    knowledge_base = KnowledgeBaseClient()
    if knowledge_base.initialize():
        print(f"📚 Knowledge base enabled ({knowledge_base.collection.count()} chunks indexed)")
    else:
        print("ℹ️ Knowledge base disabled (run 'python index_knowledge_base.py' to enable)")
        knowledge_base = None
    
    # Calculate the stale date cutoff (if STALE_THRESHOLD_DAYS is set)
    stale_cutoff_ms = None
    if STALE_THRESHOLD_DAYS and STALE_THRESHOLD_DAYS > 0:
        stale_cutoff = datetime.now() - timedelta(days=STALE_THRESHOLD_DAYS)
        stale_cutoff_ms = int(stale_cutoff.timestamp() * 1000)  # HubSpot uses milliseconds
    
    # Print filter configuration
    print(f"\n📋 Filter Configuration:")
    print(f"   - Required: lead_scoring_threshold must exist")
    print(f"   - Engagement scoring: {'HubSpot (lead_scoring_total)' if USE_HUBSPOT_SCORING else 'Custom (calculated)'}")
    print(f"   - Marketing Contacts only: {'Yes (if REQUIRE_MARKETING_CONTACT=true)' if os.getenv('REQUIRE_MARKETING_CONTACT', 'false').lower() == 'true' else 'Optional'}")
    print(f"   - Min employee size: {MIN_EMPLOYEE_SIZE if MIN_EMPLOYEE_SIZE > 0 else 'Not set (optional)'}")
    
    # Format stale threshold message
    if STALE_THRESHOLD_DAYS and STALE_THRESHOLD_DAYS > 0:
        stale_cutoff = datetime.now() - timedelta(days=STALE_THRESHOLD_DAYS)
        stale_msg = f"{STALE_THRESHOLD_DAYS} days (before {stale_cutoff.strftime('%Y-%m-%d')})"
    else:
        stale_msg = "Not set (optional)"
    print(f"   - Stale threshold: {stale_msg}")
    print(f"   - Target industries: {TARGET_INDUSTRIES if TARGET_INDUSTRIES else 'All (optional)'}")
    print(f"   - Target countries: {TARGET_COUNTRIES if TARGET_COUNTRIES else 'All (optional)'}")
    print(f"   - Target job titles: {TARGET_JOB_TITLES if TARGET_JOB_TITLES else 'All (optional)'}")
    print(f"   - Target lifecycle stages: {TARGET_LIFECYCLE_STAGES if TARGET_LIFECYCLE_STAGES else 'All (optional)'}")
    
    # Step 1: Search for contacts using HubSpot filters
    if USE_HUBSPOT_SCORING:
        print(f"\n📊 Searching HubSpot for contacts with lead_scoring_threshold...")
    else:
        print(f"\n📊 Searching HubSpot for contacts...")
    
    contact_properties = [
        "email", "firstname", "lastname", "jobtitle", "company", "country",
        "lifecyclestage", "notes_last_contacted", "hs_sales_email_last_sent",
        "notes_last_updated", "hs_email_open_count", 
        "hs_email_click_count", "hs_sales_email_last_replied",
        "hs_analytics_num_page_views", "num_conversion_events",
        "associatedcompanyid", "hs_marketable_status", "employee_size",
        "hs_linkedin_url", "lead_scoring_threshold", "lead_scoring_total",
        "lead_scoring_fit", "lead_scoring_engagement"
    ]
    
    # Build HubSpot search filters
    hubspot_filters = []
    
    # Required filter: lead_scoring_threshold must exist (only if USE_HUBSPOT_SCORING is true)
    if USE_HUBSPOT_SCORING:
        hubspot_filters.append({
            "propertyName": "lead_scoring_threshold",
            "operator": "HAS_PROPERTY"
        })
    
    # Optional filter: Marketing Contact = Yes (if enabled)
    if os.getenv("REQUIRE_MARKETING_CONTACT", "false").lower() == "true":
        hubspot_filters.append({
            "propertyName": "hs_marketable_status",
            "operator": "EQ",
            "value": "true"
        })
    
    # Optional filter: Employee Size >= MIN_EMPLOYEE_SIZE (if set)
    if MIN_EMPLOYEE_SIZE > 0:
        hubspot_filters.append({
            "propertyName": "employee_size",
            "operator": "GTE",
            "value": str(MIN_EMPLOYEE_SIZE)
        })
    
    # Optional filter: Stale threshold (if set)
    if STALE_THRESHOLD_DAYS and STALE_THRESHOLD_DAYS > 0:
        hubspot_filters.append({
            "propertyName": "notes_last_updated",
            "operator": "LT",
            "value": str(stale_cutoff_ms)
        })
    
    contacts = hubspot.search_contacts(filters=hubspot_filters, properties=contact_properties)
    print(f"   Found {len(contacts)} contacts matching HubSpot filters")
    
    # Load processed contacts log so we only ever pick the NEXT TOP_LEADS_COUNT (exclude anyone already run in a previous workflow)
    print(f"\n📋 Loading processed contacts log...")
    processed_log = load_processed_contacts_log()
    if processed_log["last_updated"]:
        print(f"   Log last updated: {processed_log['last_updated']}")
        print(f"   Already processed: {len(processed_log['processed_contact_ids'])} contacts")
    
    # Filter out already-processed contacts
    unprocessed_contacts = []
    skipped_count = 0
    for contact in contacts:
        if is_contact_already_processed(contact, processed_log):
            skipped_count += 1
            continue
        unprocessed_contacts.append(contact)
    
    already_processed_count = len(processed_log["processed_contact_ids"])
    print(f"   Skipped {skipped_count} already-processed contacts")
    print(f"   {len(unprocessed_contacts)} unprocessed contacts remaining")
    
    if not unprocessed_contacts:
        print("\n⚠️ No new contacts to process. All contacts have already been processed.")
        return []
    
    # Step 2: Sort unprocessed contacts by priority, then pick the NEXT TOP_LEADS_COUNT (so we never re-run the same contacts)
    # This is especially important when USE_HUBSPOT_SCORING is enabled - we use scores already in HubSpot
    print(f"\n📊 Sorting unprocessed contacts by priority (using HubSpot scoring data)...")
    
    if USE_HUBSPOT_SCORING:
        # When HubSpot scoring is enabled, sort using data already in HubSpot (no API calls)
        def sort_contacts_by_hubspot_priority(contact):
            props = contact.get("properties", {})
            threshold = props.get("lead_scoring_threshold", "")
            priority = get_lead_scoring_priority(threshold)
            # Get total lead score from HubSpot (already available, no calculation needed)
            total_score = 0
            hubspot_total_score = props.get("lead_scoring_total")
            if hubspot_total_score:
                try:
                    total_score = int(float(str(hubspot_total_score)))
                except (ValueError, TypeError):
                    pass
            # Lower priority number = higher priority, so sort by priority ascending, then total score descending
            return (priority, -total_score)
        
        # Sort all unprocessed contacts by HubSpot priority
        unprocessed_contacts.sort(key=sort_contacts_by_hubspot_priority)
        
        print(f"   Sorted {len(unprocessed_contacts)} unprocessed contacts by lead_scoring_threshold priority (A1→C3)")
        
        # Pick the NEXT TOP_LEADS_COUNT: first TOP_LEADS_COUNT from the sorted unprocessed list (already-processed are excluded above)
        contacts_to_process = unprocessed_contacts[:TOP_LEADS_COUNT]
        print(f"   Selected the next {TOP_LEADS_COUNT} contacts for this run (already in log: {already_processed_count} → after this run: {already_processed_count + len(contacts_to_process)})")
    else:
        # When custom scoring is used, we need to calculate scores first
        # But we can still do a quick filter before full enrichment
        contacts_to_process = unprocessed_contacts[:100]  # Process more for custom scoring to find best TOP_LEADS_COUNT
        print(f"   Will process up to {len(contacts_to_process)} contacts for custom scoring")
    
    # Step 3: Enrich ONLY the selected contacts with company data and get engagement scores
    print(f"\n🔍 Enriching {len(contacts_to_process)} selected contacts...")
    
    qualified_leads = []
    
    for contact in contacts_to_process:
        contact_id = contact["id"]
        props = contact.get("properties", {})
        contact_name = f"{props.get('firstname', '')} {props.get('lastname', '')}".strip() or "Unknown"
        
        print(f"   Processing: {contact_name}...")
        
        # Get associated company for additional context
        company = hubspot.get_associated_company(contact_id)
        company_props = company.get("properties", {}) if company else {}
        
        # Apply additional filters (industry, country, job title, lifecycle stage)
        passes, reason = passes_filters(contact, company)
        if not passes:
            print(f"      ⚠️ Skipped: {reason}")
            continue
        
        # Get score (from HubSpot total score if enabled, or calculate custom engagement score)
        if USE_HUBSPOT_SCORING:
            # Use HubSpot total lead score directly (already in contact properties, no calculation needed)
            lead_score_total = 0
            hubspot_total_score = props.get("lead_scoring_total")
            if hubspot_total_score:
                try:
                    lead_score_total = int(float(str(hubspot_total_score)))
                except (ValueError, TypeError):
                    lead_score_total = 0
            # Store in engagement_score field for backward compatibility
            engagement_score = lead_score_total
            meeting_count = 0  # Not needed when using HubSpot scoring
        else:
            # Get meeting count for custom engagement scoring
            meetings = hubspot.get_contact_meeting_associations(contact_id)
            meeting_count = len(meetings)
            # Calculate custom engagement score
            engagement_score = get_engagement_score(contact, meeting_count)
        
        # Store qualified lead with all data
        qualified_leads.append({
            "contact_id": contact_id,
            "contact": contact,
            "company": company,
            "engagement_score": engagement_score,
            "meeting_count": meeting_count if not USE_HUBSPOT_SCORING else 0,
            "contact_name": contact_name,
            "contact_email": props.get("email", "No email"),
            "contact_title": props.get("jobtitle", "Unknown"),
            "contact_linkedin_url": props.get("hs_linkedin_url", ""),
            "company_name": company_props.get("name", props.get("company", "Unknown Company")),
            "company_industry": company_props.get("industry", "Unknown"),
            "company_size": props.get("employee_size", company_props.get("numberofemployees", "Unknown")),
            "email_opens": int(props.get("hs_email_open_count", 0) or 0),
            "email_clicks": int(props.get("hs_email_click_count", 0) or 0),
            "page_views": int(props.get("hs_analytics_num_page_views", 0) or 0),
            "form_submissions": int(props.get("num_conversion_events", 0) or 0),
            "lead_scoring_threshold": props.get("lead_scoring_threshold", "N/A"),
            "lead_scoring_total": props.get("lead_scoring_total", "N/A"),
            "lead_scoring_fit": props.get("lead_scoring_fit", "N/A"),
            "lead_scoring_engagement": props.get("lead_scoring_engagement", "N/A"),
        })
        
        if USE_HUBSPOT_SCORING:
            print(f"      ✓ HubSpot Total Lead Score: {engagement_score}")
        else:
            print(f"      ✓ Calculated engagement score: {engagement_score}")
    
    # Sort qualified leads (if not already sorted by HubSpot priority)
    if USE_HUBSPOT_SCORING:
        # Already sorted by HubSpot priority before processing, but re-sort after filtering
        def sort_key(lead):
            props = lead.get("contact", {}).get("properties", {})
            threshold = props.get("lead_scoring_threshold", "")
            priority = get_lead_scoring_priority(threshold)
            # Use lead_scoring_total for sorting
            total_score = 0
            hubspot_total = props.get("lead_scoring_total")
            if hubspot_total:
                try:
                    total_score = int(float(str(hubspot_total)))
                except (ValueError, TypeError):
                    pass
            return (priority, -total_score)
        
        qualified_leads.sort(key=sort_key)
        
        # Randomly shuffle leads within the same threshold priority to ensure fair distribution
        threshold_groups = {}
        for lead in qualified_leads:
            props = lead.get("contact", {}).get("properties", {})
            threshold = props.get("lead_scoring_threshold", "UNKNOWN")
            if threshold not in threshold_groups:
                threshold_groups[threshold] = []
            threshold_groups[threshold].append(lead)
        
        # Shuffle within each threshold group
        for threshold, leads in threshold_groups.items():
            random.shuffle(leads)
        
        # Rebuild maintaining priority order (A1 highest, C3 lowest)
        qualified_leads = []
        for threshold in ["A1", "A2", "B1", "A3", "B2", "C1", "B3", "C2", "C3"]:
            if threshold in threshold_groups:
                qualified_leads.extend(threshold_groups[threshold])
    else:
        # If not using HubSpot scoring, sort by engagement score only
        qualified_leads.sort(key=lambda x: x.get("engagement_score", 0), reverse=True)
    
    # Pick top TOP_LEADS_COUNT leads
    qualified_leads = qualified_leads[:TOP_LEADS_COUNT]
    
    print(f"\n🏆 Top {len(qualified_leads)} qualified leads selected:")
    for i, lead in enumerate(qualified_leads, 1):
        props = lead.get("contact", {}).get("properties", {})
        threshold = props.get("lead_scoring_threshold", "N/A")
        if USE_HUBSPOT_SCORING:
            total_score = props.get("lead_scoring_total", "N/A")
            print(f"   {i}. {lead['contact_name']} ({lead['company_name']}) - Threshold: {threshold}, HubSpot Total Lead Score: {total_score}")
        else:
            print(f"   {i}. {lead['contact_name']} ({lead['company_name']}) - Custom Score: {lead['engagement_score']}")
    
    print(f"\n📊 Top {TOP_LEADS_COUNT} leads selected (sorted by priority A1→C3):")
    for i, lead in enumerate(qualified_leads, 1):
        props = lead.get("contact", {}).get("properties", {})
        threshold = props.get("lead_scoring_threshold", "N/A")
        print(f"   {i}. {lead['contact_name']} ({lead['company_name']}) - Threshold: {threshold}")
    
    # Split leads into Ritu and Sravan buckets (10 each by default)
    ritu_count = LEADS_PER_ASSIGNEE
    sravan_count = LEADS_PER_ASSIGNEE
    for i, lead in enumerate(qualified_leads):
        if i < ritu_count:
            lead["assigned_to"] = "ritu"
        else:
            lead["assigned_to"] = "sravan"

    ritu_leads = [lead for lead in qualified_leads[:ritu_count]]
    sravan_leads = [lead for lead in qualified_leads[ritu_count:ritu_count + sravan_count]]

    print(f"\n📊 Lead Assignment:")
    print(f"   Ritu: {len(ritu_leads)} leads")
    print(f"   Sravan: {len(sravan_leads)} leads")
    
    # Step 4: Enrich leads with context and generate emails
    print(f"\n✍️ Enriching leads and generating emails...")
    
    # Process all qualified leads (both groups, up to TOP_LEADS_COUNT)
    all_leads_to_process = qualified_leads[:TOP_LEADS_COUNT]
    final_leads = []
    
    for lead in all_leads_to_process:
        contact_name = lead["contact_name"]
        company_name = lead["company_name"]
        contact_email = lead["contact_email"]
        contact_id = lead["contact_id"]
        
        print(f"\n   Processing: {contact_name} ({company_name})")
        
        # Calculate days since activity
        props = lead["contact"].get("properties", {})
        last_activity = props.get("hs_last_sales_activity_timestamp") or props.get("notes_last_contacted")
        days_since = "30+"
        if last_activity:
            try:
                if isinstance(last_activity, str):
                    dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                else:
                    dt = datetime.fromtimestamp(last_activity / 1000)
                now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                days_since = (now - dt).days
            except (ValueError, TypeError):
                pass
        
        lead["days_since_activity"] = days_since
        
        # Apollo enrichment
        apollo_context = "Apollo integration not enabled."
        apollo_enrichment = {}
        if apollo and contact_email and contact_email != "No email":
            print(f"      🔍 Enriching via Apollo...")
            apollo_enrichment = apollo.enrich_contact(contact_email)
            apollo_context = apollo.format_apollo_context(apollo_enrichment)
            if apollo_enrichment.get("found"):
                print(f"      ✓ Apollo data found")
            else:
                print(f"      ℹ️ No Apollo data found")
        
        lead["apollo_context"] = apollo_context
        lead["apollo_enrichment"] = apollo_enrichment
        
        # Slack context
        slack_context = "Slack integration not enabled."
        if slack:
            search_terms = [t for t in [company_name, contact_name] if t and t != "Unknown"]
            all_slack_messages = []
            for term in search_terms[:2]:
                try:
                    messages = slack.search_messages(term, SLACK_CHANNELS, limit=5)
                    all_slack_messages.extend(messages)
                except Exception as e:
                    print(f"      ⚠️ Slack search error: {e}")
            
            seen_permalinks = set()
            unique_messages = []
            for msg in all_slack_messages:
                if msg.get("permalink") not in seen_permalinks:
                    seen_permalinks.add(msg.get("permalink"))
                    unique_messages.append(msg)
            
            slack_context = slack.format_slack_context(unique_messages[:10])
            if unique_messages:
                print(f"      💬 Found {len(unique_messages)} Slack messages")
        
        lead["slack_context"] = slack_context
        
        # Fireflies context: search by company name and company domain
        fireflies_context = "Fireflies integration not enabled."
        company_obj = lead.get("company")
        company_props = company_obj.get("properties", {}) if company_obj else {}
        apollo_company = (lead.get("apollo_enrichment") or {}).get("company", {})
        company_domain = extract_domain(
            company_props.get("website") or apollo_company.get("website", "")
        )
        search_terms = [t for t in [company_name, company_domain] if t and t != "Unknown Company"]
        if fireflies and search_terms:
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
                    print(f"      ⚠️ Fireflies error for '{term}': {e}")
            if all_transcripts:
                fireflies_context = fireflies.format_fireflies_context(all_transcripts[:5])
                print(f"      📞 Found {len(all_transcripts)} call transcript(s) (searched: {', '.join(search_terms)})")
            else:
                fireflies_context = "No call transcripts found."
        elif fireflies:
            fireflies_context = "No company name or domain available for Fireflies search."
        
        lead["fireflies_context"] = fireflies_context

        # Web search context
        web_research = "Web search not performed."
        if company_name and company_name != "Unknown Company":
            print(f"      🌐 Searching web for {company_name}...")
            web_research = search_company_news(company_name, parallel_key)
            if web_research and "No relevant news" not in web_research:
                print("      ✓ Found web intelligence")
            elif "not configured" in web_research:
                print("      ℹ️ Web search skipped (no PARALLEL_API_KEY)")
            else:
                print("      ℹ️ No relevant web news found")
        else:
            print("      ℹ️ Web search skipped (no company name)")
        lead["web_research"] = web_research
        
        # Get notes
        notes = hubspot.get_contact_notes(contact_id)
        notes_text = "\n".join([
            n.get("properties", {}).get("hs_note_body", "")[:500] 
            for n in notes[:3]
        ]) or "No notes available"
        lead["notes"] = notes_text
        
        # Previous emails with this contact (from HubSpot) for thread context
        try:
            previous_emails = hubspot.get_contact_emails(contact_id, limit=15)
            lead["previous_emails_context"] = format_previous_emails_context(previous_emails)
            if previous_emails:
                print(f"      📧 Found {len(previous_emails)} previous email(s) in HubSpot")
        except Exception as e:
            print(f"      ⚠️ HubSpot emails error: {e}")
            lead["previous_emails_context"] = "Could not load previous emails."
        
        # Knowledge base context (RAG)
        kb_context = "Knowledge base not available."
        if knowledge_base:
            print(f"      📚 Searching knowledge base...")
            kb_context = knowledge_base.get_context_for_lead(lead)
            if kb_context and "No relevant" not in kb_context and "not available" not in kb_context:
                print(f"      ✓ Found relevant knowledge base content")
            else:
                print(f"      ℹ️ No specific knowledge base matches")
        
        lead["knowledge_base_context"] = kb_context
        
        # Generate email
        print(f"      ✍️ Generating outreach email...")
        try:
            email_content = generate_outreach_email(claude, lead)
            
            lead["email_subject"] = email_content.get("subject", "Let's connect")
            lead["email_body"] = email_content.get("body", "")
            lead["analysis"] = email_content.get("analysis", {})
            lead["flags"] = email_content.get("flags", [])
            
            print(f"      ✓ Generated: {lead['email_subject'][:50]}...")
            
        except Exception as e:
            print(f"      ❌ Error generating email: {e}")
            lead["email_subject"] = "Let's connect"
            lead["email_body"] = "Error generating email content."
            lead["analysis"] = {}
            lead["flags"] = [f"Error: {str(e)}"]
        
        # Generate LinkedIn connection note (max 300 chars)
        print(f"      🔗 Generating LinkedIn connection note...")
        try:
            note = generate_linkedin_connection_note(claude, lead)
            lead["linkedin_connection_note"] = note
            if note:
                print(f"      ✓ LinkedIn note: {note[:60]}...")
            else:
                print(f"      ℹ️ No LinkedIn note generated")
        except Exception as e:
            print(f"      ⚠️ LinkedIn note error: {e}")
            lead["linkedin_connection_note"] = ""
        
        final_leads.append(lead)
    
    # Re-split final leads into Ritu and Sravan groups using assignment marker
    ritu_final = [lead for lead in final_leads if lead.get("assigned_to") == "ritu"]
    sravan_final = [lead for lead in final_leads if lead.get("assigned_to") == "sravan"]
    
    # Step 5: Create and send digest
    print(f"\n📧 Creating digest email...")
    
    html_digest = format_lead_digest_html(ritu_final, sravan_final)
    
    # Send the email
    if sendgrid_key:
        send_digest_email_sendgrid(LEAD_FINDER_RECIPIENTS, html_digest, sendgrid_key)
    else:
        print(f"\n⚠️ No SendGrid API key configured. Set SENDGRID_API_KEY to enable email delivery.")
    
    # Only after digest is built and sent: add this run's leads to the processed log.
    # Who gets added: exactly the TOP_LEADS_COUNT leads we processed (top N by priority, same
    # order as final_leads: first half assigned to ritu, second half to sravan). The log
    # stores contact_ids and emails in sets, so order in the JSON file is arbitrary (not "top 10"
    # then "bottom 10"). If the run had failed before this point, we would not have saved them.
    print(f"\n📝 Updating processed contacts log...")
    processed_contact_ids = processed_log["processed_contact_ids"].copy()
    processed_emails = processed_log["processed_emails"].copy()
    for lead in final_leads:
        contact_id = lead.get("contact_id")
        contact_email = lead.get("contact_email", "").lower().strip()
        if contact_id:
            processed_contact_ids.add(contact_id)
        if contact_email and contact_email != "no email":
            processed_emails.add(contact_email)
    save_processed_contacts_log(processed_contact_ids, processed_emails)
    
    print(f"\n✅ Lead Finder Agent completed successfully!")
    print(f"   Contacts matching HubSpot filters: {len(contacts)}")
    print(f"   Already processed (skipped): {skipped_count}")
    print(f"   Leads processed: {len(qualified_leads)}")
    print(f"   Emails generated: {len(final_leads)}")
    print(f"   Ritu's leads: {len(ritu_final)}")
    print(f"   Sravan's leads: {len(sravan_final)}")
    
    return {"ritu": ritu_final, "sravan": sravan_final, "all": final_leads}


if __name__ == "__main__":
    main()
