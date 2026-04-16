"""
Deck Engine — importable core logic for the Deck Creator.

Extracted from deck_creator.py for use by the Flask web app.
All functions accept an optional progress callback for real-time UI updates.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

SCOPES = [
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")
CREDS_PATH = os.path.join(BASE_DIR, "credentials.json")

TEMPLATES = {
    "implementation": {
        "name": "Implementation Walkthrough",
        "id": "1ZrZhW1cOXhwjmTSpJ7Hy6bIMduHAe8mP1lWp9yN-HfU",
        "placeholder": "Customer Name",
        "label_system": "red_and_yellow",
    },
    "psp_kickoff": {
        "name": "PSP Kickoff",
        "id": "1pGoSwBU0GmaJO7xF6PirUE_mIZPmtrGa74LspG3z-4w",
        "placeholder": "{Customer}",
        "label_system": "text_markers",
    },
}

RED_LABEL_COLOUR = {"r": (0.8, 1.0), "g": (0.0, 0.3), "b": (0.0, 0.3)}
YELLOW_LABEL_COLOUR = {"r": (0.9, 1.0), "g": (0.85, 1.0), "b": (0.0, 0.35)}

IMPLEMENTATION_FILTERS = {
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

# ---------------------------------------------------------------------------
# PSP Kickoff — tier/segment-based filtering via text markers
# ---------------------------------------------------------------------------
PSP_TIERS = ["Basic", "Advanced", "Signature"]

PSP_SEGMENTS = {
    "emerging_major_ent": "Emerging / Major ENT",
    "strategic": "Strategic",
}

# Regex patterns that match the "delete before presenting" markers in PSP slides.
# Each pattern maps to the set of tiers that should KEEP the slide.
# Matched case-insensitively against the full text of every element on a slide.
_DELETE_MARKER = re.compile(
    r"(?:"
    r"delete before presenting"
    r"|remove (?:if not (?:applicable|relevant)|before presenting)"
    r"|context may not always be available"
    r"|use this slide to start discussion"
    r")",
    re.IGNORECASE,
)

# Maps normalised marker phrases to the tiers they apply to.
# Order matters: more specific patterns first.
PSP_TIER_RULES = [
    # Multi-tier patterns first (more specific before less specific)
    (re.compile(r"for\s+advanced\s*/?\s*basic\s+customers?\s+only", re.I), {"Advanced", "Basic"}),
    (re.compile(r"for\s+advanced\s*/?\s*signature\s+customers?\s+only", re.I), {"Advanced", "Signature"}),
    # "Advanced / Signature" with spaces/slashes
    (re.compile(r"for\s+advanced\s*/\s*signature\s+customers?\s+only", re.I), {"Advanced", "Signature"}),
    # Segment-based patterns (Emerging/Major ENT, Strategic)
    (re.compile(r"(?:emerging|major\s*ent).*?\(basic\s*&\s*advanced\)", re.I), {"Basic", "Advanced"}),
    (re.compile(r"(?:emerging|major\s*ent).*?\(signature\)", re.I), {"Signature"}),
    (re.compile(r"strategic.*?\(basic\s*&\s*advanced\)", re.I), {"Basic", "Advanced"}),
    (re.compile(r"strategic.*?\(signature\)", re.I), {"Signature"}),
    # Single-tier patterns
    (re.compile(r"for\s+signature\s+customers?\s+only", re.I), {"Signature"}),
    (re.compile(r"for\s+advanced\s+customers?\s+only", re.I), {"Advanced"}),
    (re.compile(r"for\s+basic\s+customers?\s+only", re.I), {"Basic"}),
    # Roadmap / plan title-based
    (re.compile(r"roadmap\s+for\s+basic\s+plan", re.I), {"Basic"}),
    (re.compile(r"roadmap\s+for\s+advanced\s+plan", re.I), {"Advanced"}),
    (re.compile(r"roadmap\s+for\s+signature\s+plan", re.I), {"Signature"}),
    # Support team tier-specific slides
    (re.compile(r"for\s+basic\s+plan\b", re.I), {"Basic"}),
    (re.compile(r"for\s+advanced\s+plan\b", re.I), {"Advanced"}),
    (re.compile(r"for\s+signature\s+plan\b", re.I), {"Signature"}),
]

# Segment rules for slides 3-6 team composition variants
PSP_SEGMENT_RULES = [
    (re.compile(r"(?:emerging|major\s*ent)", re.I), "emerging_major_ent"),
    (re.compile(r"strategic", re.I), "strategic"),
]


@dataclass
class TextMarker:
    """A text-based conditional marker found in a PSP slide element."""
    element_id: str
    text: str
    tiers_that_keep: set
    segment: Optional[str] = None
    always_remove: bool = False


def _scan_psp_markers(slide_elements: list) -> list:
    """Find all text-based tier/segment markers in a slide's elements."""
    markers = []
    for el in slide_elements:
        eid = el["objectId"]
        text = get_element_text(el)
        if not text:
            continue

        is_delete_marker = _DELETE_MARKER.search(text)

        if not is_delete_marker:
            is_tier_title = False
            for pattern, tiers in PSP_TIER_RULES:
                if pattern.search(text):
                    is_tier_title = True
                    break
            if not is_tier_title:
                continue

        tiers_keep = set()
        segment = None
        for pattern, tiers in PSP_TIER_RULES:
            if pattern.search(text):
                tiers_keep = tiers
                break
        for pattern, seg in PSP_SEGMENT_RULES:
            if pattern.search(text):
                segment = seg
                break

        if tiers_keep or segment:
            markers.append(TextMarker(
                element_id=eid,
                text=text[:200],
                tiers_that_keep=tiers_keep,
                segment=segment,
            ))
        elif is_delete_marker:
            markers.append(TextMarker(
                element_id=eid,
                text=text[:200],
                tiers_that_keep=set(),
                always_remove=True,
            ))
    return markers


def scan_slides_psp(
    slides_service,
    presentation_id: str,
    on_progress: Callable = None,
) -> tuple:
    """PSP-specific scan. Returns (slide_infos, marker_map).

    marker_map: {slide_id: [TextMarker, ...]}
    """
    presentation = slides_service.presentations().get(
        presentationId=presentation_id
    ).execute()
    slides = presentation.get("slides", [])
    slide_infos = []
    marker_map = {}

    for idx, slide in enumerate(slides):
        info = SlideInfo(slide_id=slide["objectId"], index=idx)
        elements = slide.get("pageElements", [])

        for el in elements:
            eid = el["objectId"]
            text = get_element_text(el)
            if text and not info.title:
                shape = el.get("shape", {})
                ph = shape.get("placeholder", {})
                if ph.get("type") in ("TITLE", "CENTERED_TITLE"):
                    info.title = text
                elif len(text) > 5:
                    info.title = text[:80]
            if "{Customer}" in text or "[Customer]" in text:
                info.has_customer_placeholder = True
            if is_yellow_outline(el):
                info.yellow_outline_elements.append((eid, text))

            highlights = has_yellow_highlight_in_text(el)
            if highlights:
                info.yellow_highlights.append((eid, highlights))

            bg = get_element_bg_color(el)
            if bg and _matches_colour_range(bg, YELLOW_LABEL_COLOUR):
                info.yellow_labels.append((eid, text))

        markers = _scan_psp_markers(elements)
        if markers:
            marker_map[info.slide_id] = markers
            info.text_markers = markers

        slide_infos.append(info)

    if on_progress:
        marked = sum(1 for s in slide_infos if s.slide_id in marker_map)
        yellow_count = sum(
            len(s.yellow_labels) + len(s.yellow_highlights) + len(s.yellow_outline_elements)
            for s in slide_infos
        )
        on_progress(
            f"Scanned {len(slide_infos)} slides: "
            f"{marked} with tier/segment markers, {yellow_count} yellow elements"
        )

    return slide_infos, marker_map


def decide_psp_actions(
    slide_infos: list,
    marker_map: dict,
    tier: str,
    segment: str = None,
) -> dict:
    """Decide keep/delete for PSP slides based on tier and segment.

    - Slides with no markers: always keep
    - Slides with markers: keep if the selected tier is in the marker's tiers_that_keep
    - If a marker has a segment constraint, also check segment match
    """
    decisions = {}
    for info in slide_infos:
        markers = marker_map.get(info.slide_id)
        if not markers:
            decisions[info.slide_id] = "keep"
            continue

        tier_markers = [m for m in markers if not m.always_remove]
        if not tier_markers:
            decisions[info.slide_id] = "keep"
            continue

        should_keep = False
        for m in tier_markers:
            if tier in m.tiers_that_keep:
                if m.segment and segment:
                    if m.segment == segment:
                        should_keep = True
                elif m.segment and not segment:
                    should_keep = True
                else:
                    should_keep = True

        decisions[info.slide_id] = "keep" if should_keep else "delete"

    return decisions


def execute_psp_cleanup(
    slides_service,
    presentation_id: str,
    slide_infos: list,
    decisions: dict,
    marker_map: dict,
    on_progress: Callable = None,
) -> dict:
    """Delete non-matching slides and remove marker elements from kept slides."""
    requests = []

    slides_to_delete = sorted(
        [s for s in slide_infos if decisions.get(s.slide_id) == "delete"],
        key=lambda s: s.index,
        reverse=True,
    )
    for info in slides_to_delete:
        requests.append({"deleteObject": {"objectId": info.slide_id}})

    deleted_element_ids = set()

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            markers = marker_map.get(info.slide_id, [])
            for m in markers:
                if m.element_id not in deleted_element_ids:
                    requests.append({"deleteObject": {"objectId": m.element_id}})
                    deleted_element_ids.add(m.element_id)

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, _ in info.yellow_labels:
                if eid not in deleted_element_ids:
                    requests.append({"deleteObject": {"objectId": eid}})
                    deleted_element_ids.add(eid)

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, _ in info.yellow_outline_elements:
                if eid not in deleted_element_ids:
                    requests.append({"deleteObject": {"objectId": eid}})
                    deleted_element_ids.add(eid)

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, highlights in info.yellow_highlights:
                if eid in deleted_element_ids:
                    continue
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
                                "style": {"backgroundColor": {}},
                                "fields": "backgroundColor",
                            }
                        })

    if not requests:
        if on_progress:
            on_progress("No changes needed")
        return {}

    if on_progress:
        on_progress(
            f"Executing batch update: {len(slides_to_delete)} slide deletions, "
            f"{len(requests) - len(slides_to_delete)} marker/label removals"
        )

    result = slides_service.presentations().batchUpdate(
        presentationId=presentation_id,
        body={"requests": requests},
    ).execute()

    if on_progress:
        on_progress("Batch update complete")

    return result


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
def _rgb(color_obj) -> tuple:
    if not color_obj:
        return (0.0, 0.0, 0.0)
    rgb = color_obj.get("rgbColor", {})
    return (rgb.get("red", 0.0), rgb.get("green", 0.0), rgb.get("blue", 0.0))


def _matches_colour_range(rgb_tuple, colour_range: dict) -> bool:
    r, g, b = rgb_tuple
    return (colour_range["r"][0] <= r <= colour_range["r"][1]
            and colour_range["g"][0] <= g <= colour_range["g"][1]
            and colour_range["b"][0] <= b <= colour_range["b"][1])


def get_element_text(element) -> str:
    shape = element.get("shape", {})
    text_elements = shape.get("text", {}).get("textElements", [])
    parts = []
    for te in text_elements:
        tr = te.get("textRun", {})
        if tr.get("content"):
            parts.append(tr["content"])
    return "".join(parts).strip()


def get_element_bg_color(element) -> Optional[tuple]:
    shape = element.get("shape", {})
    bg = shape.get("shapeProperties", {}).get("solidFill", {}).get("color", {})
    if bg:
        return _rgb(bg)
    return None


def has_yellow_highlight_in_text(element) -> list:
    shape = element.get("shape", {})
    text_elements = shape.get("text", {}).get("textElements", [])
    highlighted_runs = []
    for te in text_elements:
        tr = te.get("textRun", {})
        style = tr.get("style", {})
        bg = style.get("backgroundColor", {})
        color_obj = bg.get("opaqueColor") or bg.get("color") or {}
        if color_obj and _matches_colour_range(_rgb(color_obj), YELLOW_LABEL_COLOUR):
            highlighted_runs.append(te)
    return highlighted_runs


YELLOW_OUTLINE_COLOUR = {"r": (0.9, 1.01), "g": (0.9, 1.01), "b": (0.0, 0.1)}


def get_element_outline_color(element) -> Optional[tuple]:
    shape = element.get("shape", {})
    outline = shape.get("shapeProperties", {}).get("outline", {})
    fill = outline.get("outlineFill", {}).get("solidFill", {}).get("color", {})
    if fill:
        return _rgb(fill)
    return None


def is_yellow_outline(element) -> bool:
    rgb = get_element_outline_color(element)
    if not rgb:
        return False
    return _matches_colour_range(rgb, YELLOW_OUTLINE_COLOUR)


# ---------------------------------------------------------------------------
# Authentication — two modes
# ---------------------------------------------------------------------------
def get_cached_credentials(token_json: str = None) -> Optional[Credentials]:
    """Return valid cached credentials or None.

    Args:
        token_json: Optional serialised token JSON (e.g. from Flask session).
                    Falls back to TOKEN_PATH on disk for local dev.
    """
    creds = None
    if token_json:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    elif os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _persist_token(creds)
        return creds
    return None


def _persist_token(creds: Credentials):
    """Write token to disk if possible (local dev). Silently skip on prod."""
    try:
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    except OSError:
        pass


def build_oauth_flow(redirect_uri: str) -> Flow:
    """Build an OAuth Flow for the web redirect pattern.

    Uses GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars when available
    (production on Railway), falls back to credentials.json for local dev.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")

    if client_id and client_secret:
        client_config = {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        flow = Flow.from_client_config(client_config, scopes=SCOPES)
        flow.redirect_uri = redirect_uri
        return flow

    flow = Flow.from_client_secrets_file(
        CREDS_PATH,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


def save_credentials(creds: Credentials):
    """Persist credentials to disk and return the JSON string for session storage."""
    _persist_token(creds)
    return creds.to_json()


def build_services(creds: Credentials):
    slides = build("slides", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return slides, drive


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class ColourDebugEntry:
    """Raw colour data for every coloured element — for the diagnostics panel."""
    slide_index: int
    element_id: str
    text: str
    rgb: tuple  # (r, g, b) floats 0-1
    matched_red: bool
    matched_yellow: bool


@dataclass
class SlideInfo:
    slide_id: str
    index: int
    title: str = ""
    red_labels: list = field(default_factory=list)
    yellow_labels: list = field(default_factory=list)
    yellow_highlights: list = field(default_factory=list)
    yellow_outline_elements: list = field(default_factory=list)
    has_customer_placeholder: bool = False

    # Attached by PSP scan (not set by default)
    text_markers: list = field(default_factory=list)

    def to_dict(self):
        d = {
            "slide_id": self.slide_id,
            "index": self.index,
            "title": self.title,
            "red_labels": [{"id": eid, "text": txt} for eid, txt in self.red_labels],
            "yellow_labels": [{"id": eid, "text": txt} for eid, txt in self.yellow_labels],
            "yellow_highlight_count": len(self.yellow_highlights),
            "yellow_outline_count": len(self.yellow_outline_elements),
            "has_customer_placeholder": self.has_customer_placeholder,
        }
        if self.text_markers:
            d["text_markers"] = [
                {
                    "text": m.text[:100],
                    "tiers": sorted(m.tiers_that_keep),
                    "segment": m.segment,
                    "guidance_only": m.always_remove,
                }
                for m in self.text_markers
            ]
        return d


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------
def duplicate_template(
    drive_service,
    template_id: str,
    customer_name: str,
    folder_id: str = None,
    on_progress: Callable = None,
    deck_name: str = None,
) -> str:
    name = deck_name or f"Analytics Instrumentation Walkthrough \u2014 {customer_name}"
    body = {"name": name}
    if folder_id:
        body["parents"] = [folder_id]
    copy = drive_service.files().copy(fileId=template_id, body=body).execute()
    new_id = copy["id"]
    if on_progress:
        on_progress(f"Duplicated template \u2192 {copy['name']}")
    return new_id


def replace_customer_name(
    slides_service,
    presentation_id: str,
    placeholder: str,
    customer_name: str,
    on_progress: Callable = None,
) -> int:
    placeholders = list(dict.fromkeys([
        placeholder, "{Customer}", "[Customer Name]", "[Customer]",
        "{Customer Name}", "Customer Name", "[CUSTOMER NAME]",
    ]))
    total_replaced = 0
    for ph in placeholders:
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
            count = result.get("replies", [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
            if count > 0:
                total_replaced += count
                if on_progress:
                    on_progress(f"Replaced '{ph}' \u2192 '{customer_name}' ({count} occurrences)")
        except HttpError:
            pass
    if total_replaced == 0 and on_progress:
        on_progress("Warning: no placeholder text found to replace")
    return total_replaced


def replace_customer_logo(
    slides_service,
    drive_service,
    presentation_id: str,
    logo_bytes: bytes,
    logo_mime: str,
    on_progress: Callable = None,
) -> int:
    """Upload logo to Drive and replace all 'Customer Logo' placeholder shapes.

    Uses domain-internal sharing to avoid 'publishOutNotPermitted' errors on
    Google Workspace accounts that block public file sharing.  Falls back to
    a manual find-and-replace approach if replaceAllShapesWithImage fails.

    Returns the number of occurrences replaced.
    """
    media = MediaInMemoryUpload(logo_bytes, mimetype=logo_mime, resumable=False)
    file_meta = {"name": "customer_logo_temp", "mimeType": logo_mime}
    uploaded = drive_service.files().create(
        body=file_meta, media_body=media, fields="id",
    ).execute()
    file_id = uploaded["id"]

    # Try public sharing first; if org policy blocks it, fall back to
    # domain sharing or skip (replaceAllShapesWithImage needs a readable URL).
    image_url = None
    for perm_body in [
        {"role": "reader", "type": "anyone"},
        {"role": "reader", "type": "domain", "domain": "amplitude.com"},
    ]:
        try:
            drive_service.permissions().create(
                fileId=file_id, body=perm_body,
            ).execute()
            image_url = f"https://lh3.googleusercontent.com/d/{file_id}"
            break
        except HttpError:
            continue

    if not image_url:
        if on_progress:
            on_progress(
                "Could not share logo file (org policy). "
                "Falling back to manual placeholder replacement..."
            )
        total = _replace_logo_manual(
            slides_service, presentation_id, file_id, drive_service,
            logo_bytes, logo_mime, on_progress,
        )
        _cleanup_drive_file(drive_service, file_id)
        return total

    logo_placeholders = ["Customer Logo"]
    total = 0
    for ph in logo_placeholders:
        req = {
            "replaceAllShapesWithImage": {
                "imageUrl": image_url,
                "imageReplaceMethod": "CENTER_INSIDE",
                "containsText": {"text": ph, "matchCase": False},
            }
        }
        try:
            result = slides_service.presentations().batchUpdate(
                presentationId=presentation_id,
                body={"requests": [req]},
            ).execute()
            count = result.get("replies", [{}])[0].get(
                "replaceAllShapesWithImage", {}
            ).get("occurrencesChanged", 0)
            if count > 0:
                total += count
                if on_progress:
                    on_progress(f"Replaced '{ph}' with logo image ({count} occurrences)")
        except HttpError as e:
            if on_progress:
                on_progress(f"replaceAllShapesWithImage failed for '{ph}': {e}")
            manual = _replace_logo_manual(
                slides_service, presentation_id, file_id, drive_service,
                logo_bytes, logo_mime, on_progress,
            )
            total += manual
            break

    _cleanup_drive_file(drive_service, file_id)

    if total == 0 and on_progress:
        on_progress("Warning: no 'Customer Logo' placeholders found to replace")
    return total


def _cleanup_drive_file(drive_service, file_id: str):
    try:
        drive_service.files().delete(fileId=file_id).execute()
    except HttpError:
        pass


def _replace_logo_manual(
    slides_service,
    presentation_id: str,
    file_id: str,
    drive_service,
    logo_bytes: bytes,
    logo_mime: str,
    on_progress: Callable = None,
) -> int:
    """Fallback: find 'Customer Logo' text boxes, note their position/size,
    delete them, and insert a createImage at the same location using a
    Google Drive content URL that the authenticated user can access."""

    presentation = slides_service.presentations().get(
        presentationId=presentation_id,
    ).execute()
    slides = presentation.get("slides", [])

    placeholders = []
    for slide in slides:
        page_id = slide["objectId"]
        for el in slide.get("pageElements", []):
            text = get_element_text(el)
            if text and "customer logo" in text.lower().strip():
                transform = el.get("transform", {})
                size = el.get("size", {})
                placeholders.append({
                    "element_id": el["objectId"],
                    "page_id": page_id,
                    "size": size,
                    "transform": transform,
                })

    if not placeholders:
        if on_progress:
            on_progress("No 'Customer Logo' placeholder shapes found")
        return 0

    # Use the lh3 content URL which is accessible to anyone in the same
    # Google Workspace domain without public sharing.
    image_url = f"https://lh3.googleusercontent.com/d/{file_id}"

    requests = []
    for ph in placeholders:
        requests.append({"deleteObject": {"objectId": ph["element_id"]}})
        requests.append({
            "createImage": {
                "url": image_url,
                "elementProperties": {
                    "pageObjectId": ph["page_id"],
                    "size": ph["size"],
                    "transform": ph["transform"],
                },
            }
        })

    try:
        slides_service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": requests},
        ).execute()
        if on_progress:
            on_progress(f"Replaced {len(placeholders)} logo placeholder(s) (manual method)")
        return len(placeholders)
    except HttpError as e:
        if on_progress:
            on_progress(f"Manual logo replacement also failed: {e}")
        return 0


def scan_slides(
    slides_service,
    presentation_id: str,
    on_progress: Callable = None,
) -> tuple:
    """Returns (slide_infos, colour_debug_entries)."""
    presentation = slides_service.presentations().get(
        presentationId=presentation_id
    ).execute()
    slides = presentation.get("slides", [])
    slide_infos = []
    colour_debug = []

    for idx, slide in enumerate(slides):
        info = SlideInfo(slide_id=slide["objectId"], index=idx)

        for element in slide.get("pageElements", []):
            eid = element["objectId"]
            text = get_element_text(element)
            bg = get_element_bg_color(element)

            if text and not info.title:
                shape = element.get("shape", {})
                ph = shape.get("placeholder", {})
                if ph.get("type") in ("TITLE", "CENTERED_TITLE"):
                    info.title = text
                elif len(text) > 5:
                    info.title = text[:80]

            if bg:
                is_red = _matches_colour_range(bg, RED_LABEL_COLOUR)
                is_yellow = _matches_colour_range(bg, YELLOW_LABEL_COLOUR)
                colour_debug.append(ColourDebugEntry(
                    slide_index=idx,
                    element_id=eid,
                    text=text[:120],
                    rgb=bg,
                    matched_red=is_red,
                    matched_yellow=is_yellow,
                ))
                if is_red:
                    info.red_labels.append((eid, text))
                elif is_yellow:
                    info.yellow_labels.append((eid, text))

            highlights = has_yellow_highlight_in_text(element)
            if highlights:
                info.yellow_highlights.append((eid, highlights))

            if is_yellow_outline(element):
                info.yellow_outline_elements.append((eid, text))

            if "[Customer]" in text or "{Customer}" in text:
                info.has_customer_placeholder = True

        slide_infos.append(info)

    if on_progress:
        red_count = sum(1 for s in slide_infos if s.red_labels)
        yellow_count = sum(1 for s in slide_infos if s.yellow_labels)
        on_progress(
            f"Scanned {len(slide_infos)} slides: "
            f"{red_count} with red labels, {yellow_count} with yellow labels, "
            f"{len(colour_debug)} coloured elements total"
        )

    return slide_infos, colour_debug


def decide_slide_actions(
    slide_infos: list,
    keep_keywords: list,
    remove_keywords: list,
) -> dict:
    decisions = {}
    for info in slide_infos:
        if not info.red_labels:
            decisions[info.slide_id] = "keep"
            continue

        label_texts = " ".join(text for _, text in info.red_labels).lower()
        slide_text = (info.title + " " + label_texts).lower()

        should_delete = False
        for kw in remove_keywords:
            if kw.lower() in slide_text:
                should_delete = True
                break

        if not should_delete:
            for kw in keep_keywords:
                if kw.lower() in slide_text:
                    should_delete = False
                    break

        if not should_delete and not any(kw.lower() in slide_text for kw in keep_keywords):
            decisions[info.slide_id] = "keep"
        elif should_delete:
            decisions[info.slide_id] = "delete"
        else:
            decisions[info.slide_id] = "keep"

    return decisions


def build_keep_remove_keywords(config: dict) -> tuple:
    """
    Build (keep_keywords, remove_keywords) from a web form config dict.

    config keys: sdk_types (list), import_sources (list),
                 tag_managers (list), third_party (bool), mobile_autocapture (bool)
    """
    all_options = {
        **IMPLEMENTATION_FILTERS["sdk_types"],
        **IMPLEMENTATION_FILTERS["import_sources"],
        **IMPLEMENTATION_FILTERS["tag_managers"],
        **IMPLEMENTATION_FILTERS["other"],
    }
    selected = set(config.get("sdk_types", [])
                   + config.get("import_sources", [])
                   + config.get("tag_managers", []))
    if config.get("third_party"):
        selected.add("3rd Party Analytics")
    if config.get("mobile_autocapture"):
        selected.add("Mobile Autocapture")

    keep, remove = [], []
    for name, keywords in all_options.items():
        if name in selected:
            keep.extend(keywords)
        else:
            remove.extend(keywords)
    return keep, remove


def execute_deletions_and_cleanup(
    slides_service,
    presentation_id: str,
    slide_infos: list,
    decisions: dict,
    on_progress: Callable = None,
) -> dict:
    requests = []

    slides_to_delete = sorted(
        [s for s in slide_infos if decisions.get(s.slide_id) == "delete"],
        key=lambda s: s.index,
        reverse=True,
    )
    for info in slides_to_delete:
        requests.append({"deleteObject": {"objectId": info.slide_id}})

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, _ in info.red_labels:
                requests.append({"deleteObject": {"objectId": eid}})

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, _ in info.yellow_labels:
                requests.append({"deleteObject": {"objectId": eid}})

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, highlights in info.yellow_highlights:
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
                                "style": {"backgroundColor": {}},
                                "fields": "backgroundColor",
                            }
                        })

    for info in slide_infos:
        if decisions.get(info.slide_id) == "keep":
            for eid, _ in info.yellow_outline_elements:
                requests.append({"deleteObject": {"objectId": eid}})

    if not requests:
        if on_progress:
            on_progress("No changes needed")
        return {}

    if on_progress:
        on_progress(
            f"Executing batch update: {len(slides_to_delete)} slide deletions, "
            f"{len(requests) - len(slides_to_delete)} label removals"
        )

    result = slides_service.presentations().batchUpdate(
        presentationId=presentation_id,
        body={"requests": requests},
    ).execute()

    if on_progress:
        on_progress("Batch update complete")

    return result
