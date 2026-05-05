#!/usr/bin/env python3
"""
CSV Outreach Email Generator

Reads a CSV of contacts, detects column headers automatically, and optionally enriches
contacts via Apollo (contact + company), pulls context from the local ChromaDB knowledge
base, and uses web search. Generates personalized one-on-one emails (80–100 words).

CSV requirements:
  - Header row with at least one of: email (e.g. E-mail Address, Email) or full name.
  - Rows must have a valid email address to be processed.
  - Additional columns are optional; the script recognizes many common names (company,
    job title, sector, website, LinkedIn, location, interests, etc.). More columns
    improve personalization. Unrecognized columns are ignored.

Usage:
  python csv_outreach_emails.py path/to/contacts.csv
  python csv_outreach_emails.py path/to/contacts.csv --output emails.json
  python csv_outreach_emails.py path/to/contacts.csv --limit 5   # first 5 rows only

Environment:
  ANTHROPIC_API_KEY   - Required for Claude
  APOLLO_API_KEY      - Optional; enables contact/company enrichment
  PARALLEL_API_KEY    - Optional; enables web search via Parallel.ai for extra context
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# Reuse same knowledge base as lead_finder_agent
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), ".chroma")
COLLECTION_NAME = "adopt_ai_knowledge"

# System prompt for outreach email generation
CSV_OUTREACH_SYSTEM_PROMPT = """You are an AI sales assistant for Adopt AI, specializing in generating personalized LinkedIn InMail messages. Your goal is to setup meetings with people who are attending the HumanX conference in San Francisco in April. In the LinkedIn InMail message, do mention that we are looking forward to meeting them at the conference and we'd love to know more about how Adopt AI can help. Look at the notes column for any additional information about the contact and their interests. Check for HumanX conference if required.

## Lead Context

The user will provide Lead Context in the next message, including:
- Contact (name, title, email)
- Company (name, industry, size)
- Notes from CSV (any additional information about the contact and their interests)
- Apollo enrichment data
- Adopt AI knowledge base content (relevant to this contact)

## Your Task

### Step 1: Analyze the Context

From the Apollo data and notes from CSV, identify:
1. **Interest Indicators**: What are they interested in? Look at the notes column for any additional information about the contact and their interests.
2. **Company Context**: What does Apollo tell us about their company, tech stack?
3. **Relevant Angle**: What capability or use case would resonate most?

### Step 2: Generate the Email

**InMail Message Structure (80 words MAX):**
1. **Opening**: Acknowledge their engagement naturally ("We are looking forward to meeting you at the HumanX conference in San Francisco in April..."). Look at the notes column for any additional information about the contact and their interests. Check for HumanX conference if required.
2. **Value Hook**: Connect their apparent interest to a specific capability or outcome.
3. **Simple CTA**: One clear, low-friction ask to schedule a meeting.

## Response Format

Respond with ONLY the InMail message body in plain text. Do not include:
- JSON or any structured format
- Labels like "Body:" or "Message:"
- The contact's email address

Just the message body itself, 80 words, ready to be placed in a CSV column. The email ID will be stored in a separate column.
"""

# Prompt for generating CSV-specific context from detected headers (used once per run)
CSV_CONTEXT_GENERATION_PROMPT = """You are helping configure an outreach email generator. You will be given the column headers from an uploaded CSV of contacts. Your job is to produce a short "context block" that will be injected into the system prompt for the email generator.

**Input:** A list of the CSV's column headers (as provided by the user) and which standard contact fields were detected (e.g. company, job_title, email, sector, interests).

**Output:** Write a single, concise block of text (3–8 sentences) that:
1. Summarizes what data this CSV provides (e.g. "This CSV includes company, role, sector, and stated interests in Agentic AI and RPA.").
2. Tells the email generator how to use that data: which columns to lean on for personalization, what to emphasize when present (e.g. sector, priority projects, interests), and what to avoid or keep generic when certain columns are missing.
3. Stays neutral and instructional (second person "you" or passive), so it reads as system instructions.

Do not output JSON, bullet lists, or labels. Output only the context block itself, ready to be pasted into the email generator's system prompt. Keep it under 200 words."""


# --- CSV column name normalization (allow variations) ---
COLUMN_ALIASES = {
    "company": ["company", "company name"],
    "job_title": ["job title", "jobtitle"],
    "full_name": ["full name", "fullname", "name"],
    "email": ["e-mail address", "email address", "email", "e-mail"],
    "phone": ["phone number", "phone"],
    "linkedin_url": ["linkedin url", "linkedin"],
    "website": ["website"],
    "domain": ["domain"],
    "global_hq": ["global hq", "hq"],
    "person_based_in": ["person based in", "based in", "location"],
    "sector": ["sector", "industry"],
    "personal_annual_budget": ["personal annual budget", "annual budget", "budget"],
    "purchasing_authority": ["do you hold purchasing authority?", "purchasing authority"],
    "dept_employees": ["no. of employees within your department", "department employees", "employees in department"],
    "company_revenue": ["company revenue", "revenue"],
    "total_employees": ["total no. of employees", "total employees", "employees", "company size"],
    "priority_project_1": ["priority project 1", "priority 1", "project 1"],
    "priority_project_2": ["priority project 2", "priority 2", "project 2"],
    "priority_project_3": ["priority project 3", "priority 3", "project 3"],
    "priority_project_4": ["priority project 4", "priority 4", "project 4"],
    "priority_project_5": ["priority project 5", "priority 5", "project 5"],
    "interested_agentic_ai": ["interested in agentic ai", "agentic ai"],
    "interested_rpa": ["interested in robotic process automation (rpa)", "interested in rpa", "rpa"],
    "interested_workflow": ["interested in workflow & content automation", "workflow automation", "workflow"],
}


def normalize_header(header: str) -> str:
    return header.strip().lower() if header else ""


def build_column_map(headers: list[str]) -> dict[str, int]:
    """Map normalized field names to column indices (0-based)."""
    normalized = [normalize_header(h) for h in headers]
    column_map = {}
    for field, aliases in COLUMN_ALIASES.items():
        for i, n in enumerate(normalized):
            if n in aliases or n == field:
                column_map[field] = i
                break
    # Map "Priority Investment Projects" columns P–T (indices 15–19) by header text
    for i, n in enumerate(normalized):
        if "priority" in n and ("investment" in n or "project" in n):
            if "priority_project_1" not in column_map:
                column_map["priority_project_1"] = i
            elif "priority_project_2" not in column_map and i != column_map.get("priority_project_1"):
                column_map["priority_project_2"] = i
            elif "priority_project_3" not in column_map and i not in column_map.values():
                column_map["priority_project_3"] = i
            elif "priority_project_4" not in column_map and i not in column_map.values():
                column_map["priority_project_4"] = i
            elif "priority_project_5" not in column_map and i not in column_map.values():
                column_map["priority_project_5"] = i
    # Fallback: columns P–T are often indices 15–19 (0-based)
    if len(normalized) >= 20:
        for j, key in enumerate(["priority_project_1", "priority_project_2", "priority_project_3", "priority_project_4", "priority_project_5"]):
            if key not in column_map:
                column_map[key] = 15 + j
    return column_map


def row_to_contact(row: list[str], column_map: dict[str, int]) -> dict:
    """Convert a CSV row to a contact dict using column_map."""
    def get(key: str, default: str = "") -> str:
        idx = column_map.get(key)
        if idx is None or idx >= len(row):
            return default
        val = row[idx].strip() if row[idx] is not None else ""
        return val or default

    priority_projects = []
    for k in ["priority_project_1", "priority_project_2", "priority_project_3", "priority_project_4", "priority_project_5"]:
        v = get(k)
        if v:
            priority_projects.append(v)

    return {
        "company": get("company"),
        "job_title": get("job_title"),
        "full_name": get("full_name"),
        "email": get("email"),
        "phone": get("phone"),
        "linkedin_url": get("linkedin_url"),
        "website": get("website"),
        "domain": get("domain"),
        "global_hq": get("global_hq"),
        "person_based_in": get("person_based_in"),
        "sector": get("sector"),
        "personal_annual_budget": get("personal_annual_budget"),
        "purchasing_authority": get("purchasing_authority"),
        "dept_employees": get("dept_employees"),
        "company_revenue": get("company_revenue"),
        "total_employees": get("total_employees"),
        "priority_projects": priority_projects,
        "interested_agentic_ai": get("interested_agentic_ai"),
        "interested_rpa": get("interested_rpa"),
        "interested_workflow": get("interested_workflow"),
    }


def extract_domain(url: str) -> str:
    """Extract domain from URL (e.g. https://www.acme.com/path -> acme.com)."""
    if not url or not url.strip():
        return ""
    url = url.strip()
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


# --- Apollo client (contact + company enrichment) ---
class ApolloClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.apollo.io/api/v1"
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": api_key,
        }

    def enrich_contact(self, email: str) -> dict:
        """Enrich contact by email. Returns person + organization from people/match."""
        url = f"{self.base_url}/people/match"
        params = {"email": email, "reveal_personal_emails": "false"}
        try:
            response = requests.post(url, headers=self.headers, params=params, timeout=15)
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
                    "tech_stack": organization.get("technologies", []),
                    "keywords": organization.get("keywords", []),
                    "country": organization.get("country", ""),
                    "annual_revenue": organization.get("annual_revenue", ""),
                },
                "intent_signals": {
                    "hiring_signal": bool(organization.get("current_job_openings", [])),
                    "job_openings": organization.get("current_job_openings", [])[:5],
                },
            }
        except Exception as e:
            return {"found": False, "error": str(e)}

    def enrich_company(self, domain: str) -> dict:
        """Enrich company by domain (GET organizations/enrich)."""
        if not domain:
            return {"found": False}
        url = f"{self.base_url}/organizations/enrich"
        params = {"domain": domain}
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            org = data.get("organization", {})
            return {
                "found": bool(org),
                "company": {
                    "name": org.get("name", ""),
                    "website": org.get("website_url", ""),
                    "industry": org.get("industry", ""),
                    "employee_count": org.get("estimated_num_employees", 0),
                    "funding_stage": org.get("funding_stage", ""),
                    "tech_stack": org.get("technologies", []),
                    "keywords": org.get("keywords", []),
                    "country": org.get("country", ""),
                    "annual_revenue": org.get("annual_revenue", ""),
                },
            }
        except Exception as e:
            return {"found": False, "error": str(e)}

    def format_context(self, contact_enrich: dict, company_enrich: dict) -> str:
        """Format Apollo data for the prompt."""
        lines = []
        if contact_enrich.get("found"):
            c = contact_enrich.get("contact", {})
            co = contact_enrich.get("company", {})
            if c.get("title"):
                lines.append(f"Contact (Apollo): {c.get('name', '')} – {c['title']}")
            if co.get("industry"):
                lines.append(f"Industry (Apollo): {co['industry']}")
            if co.get("employee_count"):
                lines.append(f"Employees (Apollo): {co['employee_count']}")
            if co.get("tech_stack"):
                lines.append(f"Tech: {', '.join(co['tech_stack'][:8])}")
        if company_enrich.get("found") and not contact_enrich.get("found"):
            co = company_enrich.get("company", {})
            if co.get("name"):
                lines.append(f"Company (Apollo): {co['name']}")
            if co.get("industry"):
                lines.append(f"Industry: {co['industry']}")
            if co.get("employee_count"):
                lines.append(f"Employees: {co['employee_count']}")
        return "\n".join(lines) if lines else "No Apollo enrichment available."


# --- Knowledge base (Chroma) ---
class KnowledgeBaseClient:
    def __init__(self):
        self.client = None
        self.collection = None
        self._initialized = False

    def initialize(self) -> bool:
        if self._initialized:
            return True
        try:
            import chromadb
            if not os.path.exists(CHROMA_PERSIST_DIR):
                return False
            self.client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
            self.collection = self.client.get_collection(COLLECTION_NAME)
            self._initialized = True
            return True
        except Exception:
            return False

    def search(self, query: str, n_results: int = 5) -> list[dict]:
        if not self._initialized and not self.initialize():
            return []
        try:
            results = self.collection.query(query_texts=[query], n_results=n_results)
            if not results.get("documents") or not results["documents"][0]:
                return []
            out = []
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                out.append({
                    "content": (doc or "")[:800],
                    "source": meta.get("source", "unknown"),
                    "category": meta.get("category", "general"),
                })
            return out
        except Exception:
            return []

    def get_context_for_contact(self, contact: dict, apollo_context: str) -> str:
        """Build KB context from sector, job title, and interests."""
        if not self._initialized and not self.initialize():
            return "Knowledge base not available."
        all_results = []
        sector = contact.get("sector") or ""
        if sector:
            for r in self.search(f"{sector} industry use case capabilities", n_results=3):
                all_results.append(r)
        job_title = contact.get("job_title") or ""
        if job_title:
            for r in self.search(f"{job_title} value proposition messaging", n_results=3):
                all_results.append(r)
        interests = []
        if contact.get("interested_agentic_ai"):
            interests.append("agentic AI")
        if contact.get("interested_rpa"):
            interests.append("RPA robotic process automation")
        if contact.get("interested_workflow"):
            interests.append("workflow content automation")
        if interests:
            for r in self.search(" ".join(interests) + " use case", n_results=3):
                all_results.append(r)
        for r in self.search("capabilities features platform", n_results=2):
            all_results.append(r)
        seen = set()
        unique = []
        for r in all_results:
            key = (r.get("content", "")[:80], r.get("source"))
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return self._format(unique[:8])

    def _format(self, results: list[dict]) -> str:
        if not results:
            return "No relevant knowledge base content found."
        return "\n\n---\n\n".join(
            f"[{r.get('category', 'general')}/{r.get('source', 'unknown')}]\n{r.get('content', '')}"
            for r in results
        )


# --- Optional web search (Parallel.ai Search API) ---
# https://docs.parallel.ai/search/search-quickstart
PARALLEL_SEARCH_URL = "https://api.parallel.ai/v1beta/search"
PARALLEL_SEARCH_HEADERS = {"parallel-beta": "search-extract-2025-10-10"}


def web_search(objective: str, api_key: str, max_results: int = 5, max_chars_per_result: int = 1500) -> str:
    """Return a short context string from Parallel.ai Search API. Requires PARALLEL_API_KEY."""
    if not api_key or not objective:
        return ""
    try:
        resp = requests.post(
            PARALLEL_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                **PARALLEL_SEARCH_HEADERS,
            },
            json={
                "objective": objective,
                "search_queries": [objective],
                "max_results": max_results,
                "excerpts": {"max_chars_per_result": max_chars_per_result},
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        snippets = []
        for r in data.get("results", [])[:max_results]:
            title = r.get("title", "")
            excerpts = r.get("excerpts") or []
            first_excerpt = (excerpts[0][:800] + "…") if excerpts and excerpts[0] else ""
            if title or first_excerpt:
                snippets.append(f"{title}: {first_excerpt}".strip())
        return "\n\n".join(snippets) if snippets else ""
    except Exception:
        return ""


# --- CSV context generation (one-time, from headers) ---
def generate_csv_context(
    client: anthropic.Anthropic,
    headers: list[str],
    column_map: dict[str, int],
) -> str:
    """Call Claude to generate a context block from CSV headers; used to tailor the main email prompt."""
    raw_headers = headers
    detected_fields = sorted(column_map.keys())
    user_content = f"""CSV column headers (exactly as in the file):
{chr(10).join(f'- "{h}"' for h in raw_headers)}

Standard fields detected from these headers (our internal mapping): {", ".join(detected_fields)}

Generate the context block for the email generator as described in your instructions."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=CSV_CONTEXT_GENERATION_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = (response.content[0].text or "").strip()
        # Strip any markdown or code fence
        for marker in ["```json", "```text", "```"]:
            if marker in text:
                try:
                    start = text.index(marker) + len(marker)
                    end = text.index("```", start) if "```" in text[start:] else len(text)
                    text = text[start:end].strip()
                except ValueError:
                    pass
        return text
    except Exception as e:
        return f"(CSV context generation failed: {e}. Proceeding with default instructions.)"


# --- Email generation with Claude ---
def build_contact_context(contact: dict, apollo_text: str, kb_text: str, web_snippet: str) -> str:
    """Build the context block for one contact."""
    parts = [
        "**Contact (from CSV):**",
        f"- Name: {contact.get('full_name') or 'N/A'}",
        f"- Title: {contact.get('job_title') or 'N/A'}",
        f"- Company: {contact.get('company') or 'N/A'}",
        f"- Email: {contact.get('email') or 'N/A'}",
        f"- Sector: {contact.get('sector') or 'N/A'}",
        f"- Person based in: {contact.get('person_based_in') or 'N/A'}",
        f"- Total employees: {contact.get('total_employees') or 'N/A'}",
        f"- Company revenue: {contact.get('company_revenue') or 'N/A'}",
    ]
    if contact.get("priority_projects"):
        parts.append(f"- Priority projects: {', '.join(contact['priority_projects'][:3])}")
    if contact.get("interested_agentic_ai") or contact.get("interested_rpa") or contact.get("interested_workflow"):
        interests = []
        if contact.get("interested_agentic_ai"):
            interests.append("Agentic AI")
        if contact.get("interested_rpa"):
            interests.append("RPA")
        if contact.get("interested_workflow"):
            interests.append("Workflow & Content Automation")
        parts.append(f"- Interested in: {', '.join(interests)}")
    parts.append("")
    parts.append("**Apollo enrichment:**")
    parts.append(apollo_text)
    parts.append("")
    parts.append("**Knowledge base (relevant context):**")
    parts.append(kb_text)
    if web_snippet:
        parts.append("")
        parts.append("**Web search (extra context):**")
        parts.append(web_snippet)
    return "\n".join(parts)


def generate_email(
    client: anthropic.Anthropic,
    system_prompt: str,
    contact_context: str,
    contact: dict,
) -> dict:
    """Generate email body (80 words) and return as plain text in dict."""
    user_content = f"""Use the following context to write ONE email to this contact.

Context:
{contact_context}

Respond with ONLY the email body in plain text (80 words max). No JSON, no subject line, no labels. Just the email body."""

    text = ""
    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        # Strip any markdown code fence if model wrapped output
        for marker in ["```json", "```text", "```"]:
            if marker in text:
                try:
                    start = text.index(marker) + len(marker)
                    end = text.index("```", start) if "```" in text[start:] else len(text)
                    text = text[start:end].strip()
                except ValueError:
                    pass
        return {"body": text, "error": None}
    except Exception as e:
        return {"body": "", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Generate outreach emails from CSV using Apollo, Chroma, and Claude.")
    parser.add_argument("csv_path", help="Path to the CSV file (columns from A: Company, Job Title, Full name, ...)")
    parser.add_argument("--output", "-o", default="", help="Output file: .csv for email_id,email_body columns; else JSON (default: stdout)")
    parser.add_argument("--limit", "-n", type=int, default=0, help="Process only first N rows (0 = all)")
    parser.add_argument("--no-apollo", action="store_true", help="Skip Apollo enrichment")
    parser.add_argument("--no-kb", action="store_true", help="Skip knowledge base search")
    parser.add_argument("--no-csv-context", action="store_true", help="Skip CSV header-based context generation (use default email prompt only)")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.is_file():
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY is required.", file=sys.stderr)
        sys.exit(1)

    # Load CSV
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        headers = next(reader, [])
    if not headers:
        print("Error: CSV has no header row.", file=sys.stderr)
        sys.exit(1)

    column_map = build_column_map(headers)
    if "email" not in column_map and "full_name" not in column_map:
        print("Error: Could not find required columns (e.g. E-mail Address, Full name). Check header row.", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if not row:
                continue
            rows.append(row)
    if args.limit:
        rows = rows[: args.limit]

    contacts = [row_to_contact(row, column_map) for row in rows]
    # Skip contacts without email
    contacts = [c for c in contacts if c.get("email") and "@" in c["email"]]
    if not contacts:
        print("Error: No contacts with valid email found.", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(contacts)} contacts...", file=sys.stderr)
    apollo = None if args.no_apollo else (ApolloClient(os.getenv("APOLLO_API_KEY", "")) if os.getenv("APOLLO_API_KEY") else None)
    kb = None if args.no_kb else KnowledgeBaseClient()
    if kb and not kb.initialize():
        print("Chroma KB not found or not initialized; continuing without KB context.", file=sys.stderr)
        kb = None
    parallel_key = os.getenv("PARALLEL_API_KEY", "")
    claude = anthropic.Anthropic()

    # Generate CSV-specific context from headers (one API call) and inject into system prompt
    system_prompt = CSV_OUTREACH_SYSTEM_PROMPT
    if not args.no_csv_context:
        print("Generating context from CSV headers...", file=sys.stderr)
        csv_context = generate_csv_context(claude, headers, column_map)
        if csv_context:
            system_prompt = CSV_OUTREACH_SYSTEM_PROMPT + "\n\n## CSV-specific context (for this upload)\n\n" + csv_context

    results = []
    for i, contact in enumerate(contacts):
        contact_label = contact.get("full_name") or contact.get("email") or "?"
        print(f"  [{i+1}/{len(contacts)}] {contact_label}", file=sys.stderr)
        apollo_text = "Apollo not used."
        if apollo and contact.get("email"):
            print("      Apollo: contact lookup...", file=sys.stderr)
            contact_enrich = apollo.enrich_contact(contact["email"])
            contact_found = contact_enrich.get("found", False)
            print(f"      Apollo: contact {'found' if contact_found else 'not found'}", file=sys.stderr)
            raw_domain = (contact.get("domain") or contact.get("website") or "").strip()
            domain = extract_domain(raw_domain) if raw_domain else ""
            if domain:
                print(f"      Apollo: company lookup ({domain})...", file=sys.stderr)
                company_enrich = apollo.enrich_company(domain)
                print(f"      Apollo: company {'found' if company_enrich.get('found') else 'not found'}", file=sys.stderr)
            else:
                company_enrich = {"found": False}
                print("      Apollo: no website domain, skipping company lookup", file=sys.stderr)
            apollo_text = apollo.format_context(contact_enrich, company_enrich)
        else:
            if not apollo:
                print("      Apollo: skipped (not configured or --no-apollo)", file=sys.stderr)
            else:
                print("      Apollo: skipped (no email for contact)", file=sys.stderr)
        kb_text = "Knowledge base not used."
        if kb:
            print("      KB: searching...", file=sys.stderr)
            kb_text = kb.get_context_for_contact(contact, apollo_text)
            used = "used" if (kb_text and "not available" not in kb_text.lower() and "not found" not in kb_text.lower()) else "no relevant results"
            print(f"      KB: {used}", file=sys.stderr)
        else:
            print("      KB: skipped (not configured or --no-kb)", file=sys.stderr)
        web_snippet = ""
        web_query = (contact.get("company") or contact.get("domain") or contact.get("website") or "").strip()
        if parallel_key and web_query:
            objective = f"{web_query} {contact.get('sector', '')} — company overview and recent news".strip()
            print(f"      Web search: querying ({web_query[:50]}...)...", file=sys.stderr)
            web_snippet = web_search(objective, parallel_key, max_results=3, max_chars_per_result=800)
            print(f"      Web search: {'got snippets' if web_snippet else 'no results'}", file=sys.stderr)
        else:
            if not parallel_key:
                print("      Web search: skipped (no PARALLEL_API_KEY)", file=sys.stderr)
            else:
                print("      Web search: skipped (no company name or domain)", file=sys.stderr)
        print("      Generating email...", file=sys.stderr)
        context = build_contact_context(contact, apollo_text, kb_text, web_snippet)
        gen = generate_email(claude, system_prompt, context, contact)
        if gen.get("error"):
            print(f"      Email: ERROR — {gen['error']}", file=sys.stderr)
        else:
            print("      Email: done", file=sys.stderr)
        results.append({
            "contact": {
                "full_name": contact.get("full_name"),
                "email": contact.get("email"),
                "company": contact.get("company"),
                "job_title": contact.get("job_title"),
            },
            "body": gen.get("body", ""),
            "error": gen.get("error"),
        })

    out = {"contacts_processed": len(results), "emails": results}
    if args.output:
        out_path = Path(args.output)
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["email_id", "email_body"])
                for r in results:
                    body = (r.get("body") or "").replace("\r\n", "\n")
                    writer.writerow([r["contact"].get("email") or "", body])
            print(f"Wrote {len(results)} rows to {args.output}", file=sys.stderr)
        else:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
            print(f"Wrote {len(results)} emails to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
