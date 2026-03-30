#!/usr/bin/env python3
"""
Executive Deal Summary Agent - Single Deal Test

Runs the full pipeline (context build + Claude summary) for ONE deal by name,
for faster testing. Does not send the digest email.

Usage:
    python test_executive_summary.py
    python test_executive_summary.py "Deal Name"   # Optional: specify deal name

Environment:
    TEST_DEAL_NAME - Default deal name to search for (optional if passed as argument)
    SUMMARY_STAGES - Required. Comma-separated HubSpot deal stage IDs.
    SUMMARY_STAGE_LABELS - Optional. Comma-separated display names for stages.
"""

import os
import sys
from datetime import datetime

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Import from the main agent so we reuse the same logic
from executive_summary_agent import (
    SUMMARY_STAGES,
    SUMMARY_STAGE_LABELS,
    HubSpotClient,
    build_deal_context,
    generate_deal_summary,
)

# Default test deal name (env or CLI)
TEST_DEAL_NAME = os.getenv("TEST_DEAL_NAME", "Test Deal")


def main():
    """Run executive summary pipeline for a single deal by name."""
    deal_name = sys.argv[1] if len(sys.argv) > 1 else TEST_DEAL_NAME

    print("🧪 TEST MODE - Executive Summary (Single Deal)")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🎯 Target Deal: {deal_name}")
    print("-" * 50)

    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if not hubspot_token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is required")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")

    hubspot = HubSpotClient(hubspot_token)
    claude = anthropic.Anthropic(api_key=anthropic_key)

    stage_id_to_label = dict(zip(SUMMARY_STAGES, SUMMARY_STAGE_LABELS))

    # Fetch all deals in summary stages, then filter by deal name
    print(f"\n📊 Fetching deals in configured stages...")
    deal_properties = [
        "dealname",
        "dealstage",
        "amount",
        "deal_currency_code",
        "hubspot_owner_id",
        "closedate",
        "hs_lastmodifieddate",
    ]
    try:
        all_deals = hubspot.search_deals(stages=SUMMARY_STAGES, properties=deal_properties)
    except Exception as e:
        print(f"❌ Failed to fetch deals: {e}")
        return None

    # Match by deal name (exact or contains)
    deal_name_upper = deal_name.upper()
    matches = [
        d
        for d in all_deals
        if deal_name_upper in (d.get("properties", {}).get("dealname", "") or "").upper()
    ]
    if not matches:
        print(f"❌ No deal found matching: {deal_name}")
        print(f"   Deals in pipeline: {[d.get('properties', {}).get('dealname') for d in all_deals[:10]]}...")
        return None

    deal = matches[0]
    actual_name = deal.get("properties", {}).get("dealname", "Unknown")
    stage_id = deal.get("properties", {}).get("dealstage", "")
    stage_label = stage_id_to_label.get(stage_id, stage_id)

    print(f"   ✓ Found: {actual_name} (Stage: {stage_label})")

    print(f"\n🔍 Building deal context...")
    try:
        ctx = build_deal_context(deal, hubspot, stage_label)
    except Exception as e:
        print(f"❌ Error building context: {e}")
        import traceback
        traceback.print_exc()
        return None

    if not ctx:
        print("❌ build_deal_context returned None")
        return None

    print(f"   Contact: {ctx['contact_name']}, {ctx['contact_title']}")
    print(f"   Company: {ctx['company_name']} | {ctx['company_industry']} | {ctx['company_size']}")
    print(f"   Value: {ctx['deal_value']}")
    if ctx.get("deal_hubspot_url"):
        print(f"   HubSpot: {ctx['deal_hubspot_url']}")
    print(f"   Last email: {ctx['last_email_date']} — {ctx['last_email_subject'][:50]}...")
    print(f"   Days since activity: {ctx['days_since_activity']}")

    print(f"\n✍️ Generating AI summary...")
    try:
        summary = generate_deal_summary(claude, ctx)
    except Exception as e:
        print(f"❌ Error generating summary: {e}")
        import traceback
        traceback.print_exc()
        return None

    print("\n" + "=" * 60)
    print("📋 EXECUTIVE SUMMARY (4–6 sentences)")
    print("=" * 60)
    print(summary)
    print("=" * 60)
    print("✅ Test completed successfully!")
    return {**ctx, "ai_summary": summary}


if __name__ == "__main__":
    main()
