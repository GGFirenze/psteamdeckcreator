#!/usr/bin/env python3
"""
Deck Creator — Flask Web UI
============================
Local:  python app.py            -> http://localhost:5050
Prod:   gunicorn app:app         -> Railway / any PaaS
"""

import json
import os
import re
import secrets
import time
from queue import Queue
from threading import Thread

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, Response, stream_with_context,
)

import deck_engine as engine

IS_LOCAL = os.environ.get("RAILWAY_ENVIRONMENT") is None

if IS_LOCAL:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:5050/oauth/callback",
)

# In-memory store for SSE progress per job (keyed by session id)
_progress_queues: dict[str, Queue] = {}
# Store scan results between the preview and execute phases
_scan_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _is_authenticated() -> bool:
    creds = engine.get_cached_credentials(token_json=session.get("token_json"))
    return creds is not None


def _get_services():
    creds = engine.get_cached_credentials(token_json=session.get("token_json"))
    if not creds:
        return None, None
    session["token_json"] = creds.to_json()
    return engine.build_services(creds)


# ---------------------------------------------------------------------------
# Routes — OAuth
# ---------------------------------------------------------------------------
@app.route("/oauth/start")
def oauth_start():
    flow = engine.build_oauth_flow(REDIRECT_URI)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    flow = engine.build_oauth_flow(REDIRECT_URI)
    flow.code_verifier = session.pop("code_verifier", None)
    flow.fetch_token(authorization_response=request.url)
    token_json = engine.save_credentials(flow.credentials)
    session["token_json"] = token_json
    return redirect(url_for("index"))


@app.route("/oauth/logout")
def oauth_logout():
    session.pop("token_json", None)
    try:
        if os.path.exists(engine.TOKEN_PATH):
            os.remove(engine.TOKEN_PATH)
    except OSError:
        pass
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        authenticated=_is_authenticated(),
        templates=engine.TEMPLATES,
        filters=engine.IMPLEMENTATION_FILTERS,
    )


# ---------------------------------------------------------------------------
# Routes — API (called by frontend JS)
# ---------------------------------------------------------------------------
@app.route("/api/inspect", methods=["POST"])
def api_inspect():
    """Read-only scan of a template (no duplication). Returns label inventory."""
    if not _is_authenticated():
        return jsonify(error="Not authenticated"), 401

    data = request.json
    template_key = data.get("template", "implementation")
    template = engine.TEMPLATES.get(template_key)
    if not template:
        return jsonify(error="Unknown template"), 400

    slides_svc, _ = _get_services()
    try:
        slide_infos, colour_debug = engine.scan_slides(
            slides_svc, template["id"]
        )
        slides_data = [s.to_dict() for s in slide_infos]
        debug_data = [
            {
                "slide_index": e.slide_index,
                "element_id": e.element_id,
                "text": e.text,
                "rgb": list(e.rgb),
                "hex": "#{:02x}{:02x}{:02x}".format(
                    int(e.rgb[0] * 255), int(e.rgb[1] * 255), int(e.rgb[2] * 255)
                ),
                "matched_red": e.matched_red,
                "matched_yellow": e.matched_yellow,
            }
            for e in colour_debug
        ]
        return jsonify(
            template_name=template["name"],
            template_id=template["id"],
            label_system=template.get("label_system", "unknown"),
            total_slides=len(slide_infos),
            slides=slides_data,
            colour_debug=debug_data,
        )
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/inspect/raw", methods=["POST"])
def api_inspect_raw():
    """Dump every element with any fill colour from a template. For debugging."""
    if not _is_authenticated():
        return jsonify(error="Not authenticated"), 401

    data = request.json
    template_key = data.get("template", "implementation")
    template = engine.TEMPLATES.get(template_key)
    if not template:
        return jsonify(error="Unknown template"), 400

    slides_svc, _ = _get_services()
    try:
        presentation = slides_svc.presentations().get(
            presentationId=template["id"]
        ).execute()
        slides = presentation.get("slides", [])
        elements = []
        for idx, slide in enumerate(slides):
            for el in slide.get("pageElements", []):
                text = engine.get_element_text(el)
                shape = el.get("shape", {})
                sp = shape.get("shapeProperties", {})

                bg_fill = sp.get("solidFill", {})
                bg_rgb = None
                if bg_fill.get("color"):
                    c = bg_fill["color"].get("rgbColor", {})
                    bg_rgb = [c.get("red", 0), c.get("green", 0), c.get("blue", 0)]

                outline_fill = sp.get("outline", {}).get("outlineFill", {}).get("solidFill", {})
                outline_rgb = None
                if outline_fill.get("color"):
                    c = outline_fill["color"].get("rgbColor", {})
                    outline_rgb = [c.get("red", 0), c.get("green", 0), c.get("blue", 0)]

                # Also check text run styles for coloured text or highlights
                text_colours = []
                for te in shape.get("text", {}).get("textElements", []):
                    tr = te.get("textRun", {})
                    style = tr.get("style", {})
                    fg = style.get("foregroundColor", {}).get("color", {})
                    tbg = style.get("backgroundColor", {}).get("color", {})
                    if fg.get("rgbColor") or tbg.get("rgbColor"):
                        entry = {"text": tr.get("content", "")[:60]}
                        if fg.get("rgbColor"):
                            c = fg["rgbColor"]
                            entry["fg"] = [c.get("red", 0), c.get("green", 0), c.get("blue", 0)]
                        if tbg.get("rgbColor"):
                            c = tbg["rgbColor"]
                            entry["bg"] = [c.get("red", 0), c.get("green", 0), c.get("blue", 0)]
                        text_colours.append(entry)

                if bg_rgb or outline_rgb or text_colours or text:
                    elements.append({
                        "slide": idx + 1,
                        "id": el["objectId"],
                        "text": text[:120] if text else "",
                        "bg_rgb": bg_rgb,
                        "outline_rgb": outline_rgb,
                        "text_colours": text_colours[:5] if text_colours else [],
                    })

        return jsonify(total_elements=len(elements), elements=elements)
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Phase 1: duplicate, replace name, optionally replace logo, scan slides."""
    if not _is_authenticated():
        return jsonify(error="Not authenticated"), 401

    f = request.form
    template_key = f.get("template", "implementation")
    customer_name = f.get("customer_name", "").strip()
    folder_id = f.get("folder_id", "").strip() or None

    if not customer_name:
        return jsonify(error="Customer name is required"), 400

    template = engine.TEMPLATES.get(template_key)
    if not template:
        url = f.get("custom_url", "")
        match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
        if not match:
            return jsonify(error="Invalid template or URL"), 400
        template = {
            "id": match.group(1),
            "name": "Custom Template",
            "placeholder": f.get("custom_placeholder", "Customer Name"),
            "label_system": "red_and_yellow",
        }

    is_psp = template.get("label_system") == "text_markers"

    if is_psp:
        psp_tier = f.get("psp_tier", "").strip()
        if not psp_tier:
            return jsonify(error="PSP Tier is required"), 400

    logo_file = request.files.get("customer_logo")
    logo_bytes = None
    logo_mime = None
    if logo_file and logo_file.filename:
        logo_bytes = logo_file.read()
        logo_mime = logo_file.content_type or "image/png"

    slides_svc, drive_svc = _get_services()

    steps = []

    def progress(msg):
        steps.append({"step": len(steps) + 1, "message": msg, "ts": time.time()})

    try:
        deck_name = None
        if is_psp:
            deck_name = f"Premium Success Kickoff \u2014 {customer_name}"

        progress("Duplicating template...")
        new_id = engine.duplicate_template(
            drive_svc, template["id"], customer_name, folder_id,
            on_progress=progress, deck_name=deck_name,
        )

        progress("Replacing customer name placeholders...")
        engine.replace_customer_name(
            slides_svc, new_id, template.get("placeholder", "Customer Name"),
            customer_name, on_progress=progress,
        )

        if logo_bytes:
            progress("Replacing customer logo placeholders...")
            engine.replace_customer_logo(
                slides_svc, drive_svc, new_id,
                logo_bytes, logo_mime, on_progress=progress,
            )

        if is_psp:
            progress("Scanning slides for tier/segment markers...")
            slide_infos, marker_map = engine.scan_slides_psp(
                slides_svc, new_id, on_progress=progress
            )

            psp_tier = f.get("psp_tier")
            psp_segment = f.get("psp_segment") or None
            decisions = engine.decide_psp_actions(
                slide_infos, marker_map, psp_tier, psp_segment
            )

            job_id = secrets.token_hex(8)
            _scan_cache[job_id] = {
                "presentation_id": new_id,
                "slide_infos": slide_infos,
                "decisions": decisions,
                "flow": "psp",
                "marker_map": marker_map,
            }

            slides_data = []
            for info in slide_infos:
                d = info.to_dict()
                d["decision"] = decisions.get(info.slide_id, "keep")
                slides_data.append(d)

            to_delete = sum(1 for d in decisions.values() if d == "delete")
            progress(f"Preview ready: {to_delete} slides to delete, {len(slide_infos) - to_delete} to keep")

            return jsonify(
                job_id=job_id,
                presentation_id=new_id,
                presentation_url=f"https://docs.google.com/presentation/d/{new_id}/edit",
                slides=slides_data,
                colour_debug=[],
                steps=steps,
                thresholds={"red": engine.RED_LABEL_COLOUR, "yellow": engine.YELLOW_LABEL_COLOUR},
            )

        else:
            progress("Scanning all slides for coloured labels...")
            slide_infos, colour_debug = engine.scan_slides(
                slides_svc, new_id, on_progress=progress
            )

            config = {
                "sdk_types": f.getlist("sdk_types"),
                "import_sources": f.getlist("import_sources"),
                "tag_managers": f.getlist("tag_managers"),
                "third_party": bool(f.get("third_party")),
                "mobile_autocapture": bool(f.get("mobile_autocapture")),
            }
            keep_kw, remove_kw = engine.build_keep_remove_keywords(config)
            decisions = engine.decide_slide_actions(slide_infos, keep_kw, remove_kw)

            job_id = secrets.token_hex(8)
            _scan_cache[job_id] = {
                "presentation_id": new_id,
                "slide_infos": slide_infos,
                "decisions": decisions,
                "flow": "implementation",
            }

            slides_data = []
            for info in slide_infos:
                d = info.to_dict()
                d["decision"] = decisions.get(info.slide_id, "keep")
                slides_data.append(d)

            debug_data = [
                {
                    "slide_index": e.slide_index,
                    "element_id": e.element_id,
                    "text": e.text,
                    "rgb": list(e.rgb),
                    "hex": "#{:02x}{:02x}{:02x}".format(
                        int(e.rgb[0] * 255), int(e.rgb[1] * 255), int(e.rgb[2] * 255)
                    ),
                    "matched_red": e.matched_red,
                    "matched_yellow": e.matched_yellow,
                }
                for e in colour_debug
            ]

            to_delete = sum(1 for d in decisions.values() if d == "delete")
            progress(f"Preview ready: {to_delete} slides to delete, {len(slide_infos) - to_delete} to keep")

            return jsonify(
                job_id=job_id,
                presentation_id=new_id,
                presentation_url=f"https://docs.google.com/presentation/d/{new_id}/edit",
                slides=slides_data,
                colour_debug=debug_data,
                steps=steps,
                thresholds={
                    "red": engine.RED_LABEL_COLOUR,
                    "yellow": engine.YELLOW_LABEL_COLOUR,
                },
            )

    except Exception as e:
        return jsonify(error=str(e), steps=steps), 500


@app.route("/api/execute", methods=["POST"])
def api_execute():
    """Phase 2: apply the decisions (with optional user overrides)."""
    if not _is_authenticated():
        return jsonify(error="Not authenticated"), 401

    data = request.json
    job_id = data.get("job_id")
    overrides = data.get("overrides", {})

    cached = _scan_cache.pop(job_id, None)
    if not cached:
        return jsonify(error="Scan session expired. Please re-scan."), 404

    decisions = cached["decisions"]
    decisions.update(overrides)

    slides_svc, _ = _get_services()

    try:
        if cached.get("flow") == "psp":
            result = engine.execute_psp_cleanup(
                slides_svc,
                cached["presentation_id"],
                cached["slide_infos"],
                decisions,
                cached.get("marker_map", {}),
            )
        else:
            result = engine.execute_deletions_and_cleanup(
                slides_svc,
                cached["presentation_id"],
                cached["slide_infos"],
                decisions,
            )
        total = len(cached["slide_infos"])
        deleted = sum(1 for v in decisions.values() if v == "delete")
        return jsonify(
            success=True,
            presentation_url=f"https://docs.google.com/presentation/d/{cached['presentation_id']}/edit",
            total_slides=total,
            deleted=deleted,
            remaining=total - deleted,
        )
    except Exception as e:
        return jsonify(error=str(e)), 500


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n  Deck Creator Web UI")
    print("  http://localhost:5050\n")
    app.run(debug=True, port=5050)
