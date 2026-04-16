#!/usr/bin/env python3
"""
Deck Creator — Google Slides API Edition
=========================================
Automates customisation of Amplitude's Implementation Walkthrough and PSP Kickoff
templates using the Google Slides + Drive APIs.

Setup (one-time):
  1. Go to https://console.cloud.google.com/
  2. Create a project (or use an existing one)
  3. Enable the "Google Slides API" and "Google Drive API"
  4. Go to APIs & Services → Credentials → Create Credentials → OAuth client ID
     - Application type: Desktop app
     - Download the JSON and save it as  credentials.json  in this script's directory
  5. pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

Usage:
  python deck_creator.py

The script will walk you through the options interactively.
"""

import json
import re
import sys
import os
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Google API imports — fail gracefully with install instructions
# ---------------------------------------------------------------------------
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    sys.exit(1)

# If modifying these scopes, delete token.json to re-authenticate
SCOPES = [
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive",
]

# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------
TEMPLATES = {
    "implementation": {
        "name": "Implementation Walkthrough",
        "id": "1ZrZhW1cOXhwjmTSpJ7Hy6bIMduHAe8mP1lWp9yN-HfU",
        "placeholder": "Customer Name",
        "label_system": "red_and_yellow",   # red = conditional, yellow = informational
    },
    "psp_kickoff": {
        "name": "PSP Kickoff",
        "id": "1phWwO6mp5RBCyVEDnCPoyQMQR-8Bi8yTnIyt8ZPKPRA",
        "placeholder": "{Customer}",
        "label_system": "yellow_only",      # yellow banners for tiers + guidance
    },
}

# Colour-matching thresholds (RGB 0-1 scale)
# Google Slides stores colours as floats 0.0–1.0
RED_LABEL_COLOUR   = {"r": (0.8, 1.0), "g": (0.0, 0.3), "b": (0.0, 0.3)}
YELLOW_LABEL_COLOUR = {"r": (0.9, 1.0), "g": (0.85, 1.0), "b": (0.0, 0.35)}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def authenticate():
    """Authenticate via OAuth browser flow. Caches token in token.json."""
    creds = None
    token_path = os.path.join(os.path.dirname(__file__) or ".", "token.json")
    creds_path = os.path.join(os.path.dirname(__file__) or ".", "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_path):
                print(f"\n❌ Missing {creds_path}")
                print("Download your OAuth client JSON from GCP Console and save it as credentials.json")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as token:
            token.write(creds.to_json())

    slides_service = build("slides", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return slides_service, drive_service


# ---------------------------------------------------------------------------
# Helpers — colour matching
# ---------------------------------------------------------------------------
def _rgb(color_obj) -> tuple:
    """Extract (r, g, b) from a Slides API color object, defaulting to 0."""
    if not color_obj:
        return (0, 0, 0)
    rgb = color_obj.get("rgbColor", {})
    return (rgb.get("red", 0), rgb.get("green", 0), rgb.get("blue", 0))


def _matches_colour_range(rgb_tuple, colour_range: dict) -> bool:
    r, g, b = rgb_tuple
    return (colour_range["r"][0] <= r <= colour_range["r"][1]
            and colour_range["g"][0] <= g <= colour_range["g"][1]
            and colour_range["b"][0] <= b <= colour_range["b"][1])


def is_red_label(element) -> bool:
    """Check if an element is a red label (shape with red background or red text)."""
    shape = element.get("shape", {})
    # Check shape background fill
    bg = shape.get("shapeProperties", {}).get("solidFill", {}).get("color", {})
    if bg and _matches_colour_range(_rgb(bg), RED_LABEL_COLOUR):
        return True
    # Check if it's a text box with red background
    sp = shape.get("shapeProperties", {})
    outline_fill = sp.get("outline", {}).get("outlineFill", {}).get("solidFill", {}).get("color", {})
    if outline_fill and _matches_colour_range(_rgb(outline_fill), RED_LABEL_COLOUR):
        return True
    return False


def is_yellow_label(element) -> bool:
    """Check if an element is a yellow label (shape with yellow background)."""
    shape = element.get("shape", {})
    bg = shape.get("shapeProperties", {}).get("solidFill", {}).get("color", {})
    if bg and _matches_colour_range(_rgb(bg), YELLOW_LABEL_COLOUR):
        return True
    return False


def get_element_text(element) -> str:
    """Extract plain text from a shape element."""
    shape = element.get("shape", {})
    text_elements = shape.get("text", {}).get("textElements", [])
    parts = []
    for te in text_elements:
        tr = te.get("textRun", {})
        if tr.get("content"):
            parts.append(tr["content"])
    return "".join(parts).strip()


def get_element_bg_color(element) -> Optional[tuple]:
    """Get the background colour of a shape as (r, g, b) tuple."""
    shape = element.get("shape", {})
    bg = shape.get("shapeProperties", {}).get("solidFill", {}).get("color", {})
    if bg:
        return _rgb(bg)
    return None


def has_yellow_highlight_in_text(element) -> list:
    """Find text runs with yellow highlight (background color) and return their indices."""
    shape = element.get("shape", {})
    text_elements = shape.get("text", {}).get("textElements", [])
    highlighted_runs = []
    for te in text_elements:
        tr = te.get("textRun", {})
        style = tr.get("style", {})
        bg = style.get("backgroundColor", {}).get("color", {})
        if bg and _matches_colour_range(_rgb(bg), YELLOW_LABEL_COLOUR):
            highlighted_runs.append(te)
    return highlighted_runs


# ---------------------------------------------------------------------------
# Step 1: Duplicate template
# ---------------------------------------------------------------------------
def duplicate_template(drive_service, template_id: str, customer_name: str, folder_id: str = None) -> str:
    """Copy the template and rename with customer name. Returns new presentation ID."""
    body = {"name": f"Analytics Instrumentation Walkthrough — {customer_name}"}
    if folder_id:
        body["parents"] = [folder_id]

    copy = drive_service.files().copy(fileId=template_id, body=body).execute()
    new_id = copy["id"]
    print(f"  ✅ Duplicated template → {copy['name']}")
    print(f"     https://docs.google.com/presentation/d/{new_id}/edit")
    return new_id


# ---------------------------------------------------------------------------
# Step 2: Find & Replace customer name
# ---------------------------------------------------------------------------
def replace_customer_name(slides_service, presentation_id: str, placeholder: str, customer_name: str):
    """Replace all instances of the placeholder with the customer name."""
    # Try multiple common placeholder variants
    placeholders = [
        placeholder,
        "{Customer}",
        "[Customer Name]",
        "[Customer]",
        "{Customer Name}",
        "Customer Name",
        "[CUSTOMER NAME]",
    ]
    # De-duplicate while preserving order
    seen = set()
    unique_placeholders = []
    for p in placeholders:
        if p not in seen:
            seen.add(p)
            unique_placeholders.append(p)

    total_replaced = 0
    for ph in unique_placeholders:
        request = {
            "replaceAllText": {
                "containsText": {"text": ph, "matchCase": False},
                "replaceText": customer_name,
            }
        }
        try:
            result = slides_service.presentations().batchUpdate(
                presentationId=presentation_id,
                body={"requests": [request]},
            ).execute()
            replies = result.get("replies", [{}])
            count = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
            if count > 0:
                total_replaced += count
                print(f"  ✅ Replaced '{ph}' → '{customer_name}' ({count} occurrences)")
        except HttpError:
            pass  # Placeholder not found — that's fine

    if total_replaced == 0:
        print("  ⚠️  No placeholder text found to replace. Check the template.")
    return total_replaced


# ---------------------------------------------------------------------------
# Step 3: Scan slides — build a map of labels per slide
# ---------------------------------------------------------------------------
@dataclass
class SlideInfo:
    slide_id: str
    index: int
    title: str = ""
    red_labels: list = field(default_factory=list)      # list of (element_id, text)
    yellow_labels: list = field(default_factory=list)    # list of (element_id, text)
    yellow_highlights: list = field(default_factory=list) # elements with yellow text highlights
    has_customer_placeholder: bool = False


def scan_slides(slides_service, presentation_id: str) -> list:
    """Read all slides and categorise their labels."""
    presentation = slides_service.presentations().get(
        presentationId=presentation_id
    ).execute()
    slides = presentation.get("slides", [])
    slide_infos = []

    for idx, slide in enumerate(slides):
        info = SlideInfo(slide_id=slide["objectId"], index=idx)

        for element in slide.get("pageElements", []):
            eid = element["objectId"]
            text = get_element_text(element)
            bg = get_element_bg_color(element)

            # Detect title (first large text element)
            if text and not info.title:
                shape = element.get("shape", {})
                ph = shape.get("placeholder", {})
                if ph.get("type") in ("TITLE", "CENTERED_TITLE"):
                    info.title = text
                elif len(text) > 5 and not info.title:
                    info.title = text[:80]

            # Classify coloured labels
            if bg:
                if _matches_colour_range(bg, RED_LABEL_COLOUR):
                    info.red_labels.append((eid, text))
                elif _matches_colour_range(bg, YELLOW_LABEL_COLOUR):
                    info.yellow_labels.append((eid, text))

            # Check for yellow highlighted text runs within non-label elements
            highlights = has_yellow_highlight_in_text(element)
            if highlights:
                info.yellow_highlights.append((eid, highlights))

            # Check for remaining customer placeholders
            if "[Customer]" in text or "{Customer}" in text:
                info.has_customer_placeholder = True

        slide_infos.append(info)

    return slide_infos


# ---------------------------------------------------------------------------
# Step 4: Filter slides — decide keep/delete based on customer config
# ---------------------------------------------------------------------------
def decide_slide_actions(slide_infos: list, keep_keywords: list, remove_keywords: list) -> dict:
    """
    For each slide with a red label, decide whether to keep or delete it.

    Returns dict: {slide_id: "keep" | "delete"}
    Slides without red labels are always kept.
    """
    decisions = {}

    for info in slide_infos:
        if not info.red_labels:
            decisions[info.slide_id] = "keep"
            continue

        # Check if any red label text matches a remove keyword
        label_texts = " ".join(text for _, text in info.red_labels).lower()
        slide_text = (info.title + " " + label_texts).lower()

        should_delete = False

        # If label says "Remove if not applicable" → check if content matches keep_keywords
        for kw in remove_keywords:
            if kw.lower() in slide_text:
                should_delete = True
                break

        if not should_delete:
            for kw in keep_keywords:
                if kw.lower() in slide_text:
                    should_delete = False
                    break

        # Default: if it has a red label and no keep keyword matches, flag for review
        if not should_delete and not any(kw.lower() in slide_text for kw in keep_keywords):
            # Conservative: keep and let user decide
            decisions[info.slide_id] = "keep"
        elif should_delete:
            decisions[info.slide_id] = "delete"
        else:
            decisions[info.slide_id] = "keep"

    return decisions


# ---------------------------------------------------------------------------
# Step 5: Execute batch operations
# ---------------------------------------------------------------------------
def execute_deletions_and_cleanup(
    slides_service,
    presentation_id: str,
    slide_infos: list,
    decisions: dict,
):
    """
    Batch-delete slides and remove coloured labels in a single API call.
    This is the magic — instead of click-by-click, one batchUpdate does it all.
    """
    requests = []

    # 1. Delete slides marked for removal (process in reverse order for safety)
    slides_to_delete = [
        info for info in slide_infos
        if decisions.get(info.slide_id) == "delete"
    ]
    # Sort by index descending (highest first) to avoid index shifting
    slides_to_delete.sort(key=lambda s: s.index, reverse=True)

    for info in slides_to_delete:
        requests.append({
            "deleteObject": {"objectId": info.slide_id}
        })

    # 2. Remove red label elements from kept slides
    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, text in info.red_labels:
                requests.append({
                    "deleteObject": {"objectId": eid}
                })

    # 3. Remove yellow label elements from all kept slides
    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, text in info.yellow_labels:
                requests.append({
                    "deleteObject": {"objectId": eid}
                })

    # 4. Remove yellow text highlights from kept slides
    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, highlights in info.yellow_highlights:
                # Clear the background color on highlighted text runs
                for te in highlights:
                    start_idx = te.get("startIndex", 0)
                    end_idx = te.get("endIndex", start_idx)
                    if end_idx > start_idx:
                        requests.append({
                            "updateTextStyle": {
                                "objectId": eid,
                                "textRange": {
                                    "type": "FIXED_RANGE",
                                    "startIndex": start_idx,
                                    "endIndex": end_idx,
                                },
                                "style": {
                                    "backgroundColor": {}  # empty = clear highlight
                                },
                                "fields": "backgroundColor",
                            }
                        })

    if not requests:
        print("  ℹ️  No changes needed.")
        return

    print(f"\n  📦 Sending batch update: {len(requests)} operations...")
    print(f"     - {len(slides_to_delete)} slides to delete")
    print(f"     - {len(requests) - len(slides_to_delete)} label elements to remove")

    result = slides_service.presentations().batchUpdate(
        presentationId=presentation_id,
        body={"requests": requests},
    ).execute()

    print(f"  ✅ Batch update complete!")
    return result


# ---------------------------------------------------------------------------
# Implementation Walkthrough — specific filtering logic
# ---------------------------------------------------------------------------
IMPLEMENTATION_FILTERS = {
    # Maps user-facing option → keywords that indicate slides to KEEP
    "sdk_types": {
        "Browser SDK": ["browser sdk", "browser", "javascript", "js sdk"],
        "Mobile SDKs": ["mobile sdk", "android", "ios", "react native", "flutter"],
        "Server-Side SDKs": ["server-side", "server side", "java", "node", "go", "python"],
    },
    "import_sources": {
        "Snowflake Import": ["snowflake"],
        "BigQuery Import": ["bigquery", "big query"],
        "Databricks Import": ["databricks"],
        "Amazon S3 Import": ["amazon s3", "s3 import"],
        "Google Cloud Storage Import": ["google cloud storage", "gcs import"],
    },
    "tag_managers": {
        "Google Tag Manager": ["google tag manager", "gtm"],
        "Tealium": ["tealium"],
    },
    "other": {
        "3rd Party Analytics": ["3rd party", "third party", "third-party"],
        "Mobile Autocapture": ["mobile autocapture"],
    },
}


def get_implementation_config() -> tuple:
    """Interactive prompts to gather Implementation Walkthrough config."""
    print("\n📋 Implementation Walkthrough Configuration")
    print("=" * 50)

    customer_name = input("\n  Customer name: ").strip()

    print("\n  Which SDK types will the customer use?")
    print("  (comma-separated, e.g.: 1,3)")
    sdk_options = list(IMPLEMENTATION_FILTERS["sdk_types"].keys())
    for i, opt in enumerate(sdk_options, 1):
        print(f"    {i}. {opt}")
    sdk_input = input("  → ").strip()
    selected_sdks = [sdk_options[int(x.strip()) - 1] for x in sdk_input.split(",") if x.strip().isdigit() and 0 < int(x.strip()) <= len(sdk_options)]

    print("\n  Which import sources? (leave blank for none)")
    import_options = list(IMPLEMENTATION_FILTERS["import_sources"].keys())
    for i, opt in enumerate(import_options, 1):
        print(f"    {i}. {opt}")
    import_input = input("  → ").strip()
    selected_imports = []
    if import_input:
        selected_imports = [import_options[int(x.strip()) - 1] for x in import_input.split(",") if x.strip().isdigit() and 0 < int(x.strip()) <= len(import_options)]

    print("\n  Which tag managers? (leave blank for none)")
    tm_options = list(IMPLEMENTATION_FILTERS["tag_managers"].keys())
    for i, opt in enumerate(tm_options, 1):
        print(f"    {i}. {opt}")
    tm_input = input("  → ").strip()
    selected_tms = []
    if tm_input:
        selected_tms = [tm_options[int(x.strip()) - 1] for x in tm_input.split(",") if x.strip().isdigit() and 0 < int(x.strip()) <= len(tm_options)]

    use_3rd_party = input("\n  Using 3rd party analytics? (y/n): ").strip().lower() == "y"
    use_mobile_autocapture = "Mobile SDKs" in selected_sdks and input("  Using mobile autocapture? (y/n): ").strip().lower() == "y"

    # Build keep/remove keyword lists
    keep_keywords = []
    remove_keywords = []

    all_options = {
        **IMPLEMENTATION_FILTERS["sdk_types"],
        **IMPLEMENTATION_FILTERS["import_sources"],
        **IMPLEMENTATION_FILTERS["tag_managers"],
        **IMPLEMENTATION_FILTERS["other"],
    }

    selected = set(selected_sdks + selected_imports + selected_tms)
    if use_3rd_party:
        selected.add("3rd Party Analytics")
    if use_mobile_autocapture:
        selected.add("Mobile Autocapture")

    for option_name, keywords in all_options.items():
        if option_name in selected:
            keep_keywords.extend(keywords)
        else:
            remove_keywords.extend(keywords)

    return customer_name, keep_keywords, remove_keywords


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("  🎯 Deck Creator — Google Slides API Edition")
    print("=" * 60)

    # Authenticate
    print("\n🔐 Authenticating with Google...")
    slides_service, drive_service = authenticate()
    print("  ✅ Authenticated successfully")

    # Choose template
    print("\n📑 Choose a template:")
    print("  1. Implementation Walkthrough")
    print("  2. PSP Kickoff")
    print("  3. Custom URL")
    choice = input("  → ").strip()

    if choice == "1":
        template = TEMPLATES["implementation"]
    elif choice == "2":
        template = TEMPLATES["psp_kickoff"]
        print("\n  ⚠️  PSP Kickoff filtering not yet implemented. Coming soon!")
        sys.exit(0)
    elif choice == "3":
        url = input("  Paste the Google Slides URL: ").strip()
        match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
        if not match:
            print("  ❌ Couldn't extract presentation ID from URL")
            sys.exit(1)
        template = {
            "id": match.group(1),
            "name": "Custom Template",
            "placeholder": input("  What placeholder text does it use for customer name? ").strip(),
            "label_system": "red_and_yellow",
        }
    else:
        print("  ❌ Invalid choice")
        sys.exit(1)

    # Gather config
    if choice == "1":
        customer_name, keep_keywords, remove_keywords = get_implementation_config()
    else:
        customer_name = input("\n  Customer name: ").strip()
        keep_keywords, remove_keywords = [], []

    # Optional: target folder
    folder_id = None
    folder_input = input("\n  Google Drive folder ID to save in (leave blank for My Drive): ").strip()
    if folder_input:
        folder_id = folder_input

    # Execute
    print("\n" + "=" * 60)
    print("  🚀 Starting deck creation...")
    print("=" * 60)

    # Step 1: Duplicate
    print("\n📄 Step 1: Duplicating template...")
    new_id = duplicate_template(drive_service, template["id"], customer_name, folder_id)

    # Step 2: Replace customer name
    print("\n✏️  Step 2: Replacing customer name placeholders...")
    replace_customer_name(slides_service, new_id, template.get("placeholder", "Customer Name"), customer_name)

    # Step 3: Scan slides
    print("\n🔍 Step 3: Scanning all slides...")
    slide_infos = scan_slides(slides_service, new_id)
    print(f"  Found {len(slide_infos)} slides")

    red_count = sum(1 for s in slide_infos if s.red_labels)
    yellow_count = sum(1 for s in slide_infos if s.yellow_labels)
    highlight_count = sum(1 for s in slide_infos if s.yellow_highlights)
    print(f"  - {red_count} slides with red labels")
    print(f"  - {yellow_count} slides with yellow labels")
    print(f"  - {highlight_count} slides with yellow text highlights")

    # Step 4: Decide which slides to keep/delete
    print("\n🧹 Step 4: Filtering slides...")
    decisions = decide_slide_actions(slide_infos, keep_keywords, remove_keywords)

    to_delete = [s for s in slide_infos if decisions.get(s.slide_id) == "delete"]
    print(f"  Will delete {len(to_delete)} slides:")
    for s in to_delete:
        label_text = s.red_labels[0][1][:60] if s.red_labels else "no label"
        print(f"    - Slide {s.index + 1}: {s.title[:50]} [{label_text}]")

    confirm = input("\n  Proceed with deletions and label cleanup? (y/n): ").strip().lower()
    if confirm != "y":
        print("  Aborted. Your duplicated deck is still available with no changes.")
        sys.exit(0)

    # Step 5: Execute everything in one batch
    print("\n⚡ Step 5: Executing batch operations...")
    execute_deletions_and_cleanup(slides_service, new_id, slide_infos, decisions)

    # Summary
    remaining = len(slide_infos) - len(to_delete)
    print("\n" + "=" * 60)
    print(f"  🎉 Done! Your deck is ready for review.")
    print(f"     {remaining} slides remaining (deleted {len(to_delete)})")
    print(f"     https://docs.google.com/presentation/d/{new_id}/edit")
    print("=" * 60)
    print("\n  Next steps:")
    print("  1. Open the link above")
    print("  2. Quick scan for any edge cases the script missed")
    print("  3. Insert customer logo if needed")
    print("  4. Present! 🚀\n")


if __name__ == "__main__":
    main()
