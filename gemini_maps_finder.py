#!/usr/bin/env python3
"""
gemini_maps_finder.py — Finds candidate companies using Gemini's
"Grounding with Google Maps" tool on Vertex AI, instead of web search
grounding (gemini_search_finder.py) or a company database (PDL/Apollo).

WHY THIS EXISTS: gemini_search_finder.py uses Grounding with Google Search,
which is built for factual Q&A / well-indexed pages — it's genuinely weak
at surfacing small, low-web-presence local businesses, and in testing
produced results with empty `domain` fields (unverifiable, sometimes
fabricated-adjacent). Grounding with Google Maps queries Google's actual
place database (250M+ real business listings: name, address, website,
category, ratings) instead of the open web, which is the structurally
correct source for "find small local businesses matching criteria."

IMPORTANT CAVEAT — read before assuming this "just works" better:
Maps grounding is anchored to a location. Per Google's own docs, "near me"
-style queries use the lat/lng you provide; broad non-local queries ("small
businesses in India") are largely uninfluenced by it and behave more like
an ungrounded/Search-like query. This script therefore loops over a list
of city anchors (lat/lng) rather than firing one country-wide query — this
is a real architectural difference from gemini_search_finder.py, not a
minor flag change. Expect it to find things per-city, not nationally in
one shot.

Also identical to gemini_search_finder.py: Gemini's responseSchema
enforcement does not currently combine with search-type tools (googleMaps,
googleSearch) in the same generateContent call. This script uses the same
two-call split — one grounded call per city to gather real listings, one
non-grounded call to reshape that into structured JSON.

Billing/quota: same Vertex AI free grounded-request allowance as
gemini_search_finder.py (1,500/day, up to 45,000/month) — each city anchor
is one grounded request, so N cities = N requests, not 1.

Setup: identical to gemini_search_finder.py —
    1. gcloud auth application-default login
    2. gcloud config set project YOUR_PROJECT_ID
    3. pip install requests google-auth

Usage:
    python gemini_maps_finder.py --out prospects.csv \
        --project YOUR_PROJECT_ID \
        --description "small businesses (2-50 employees) that would benefit from AI automation" \
        --cities "Bengaluru,Chennai,Hyderabad,Pune,Mumbai" \
        --count-per-city 8

    Or pass your own lat/lng anchors instead of --cities:
    python gemini_maps_finder.py --out prospects.csv --project YOUR_PROJECT_ID \
        --description "..." --anchors "12.9716,77.5946:Bengaluru;19.0760,72.8777:Mumbai"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests
import google.auth
import google.auth.transport.requests

LOCATION = "us-central1"
VERTEX_ENDPOINT_TEMPLATE = (
    "https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/{location}/publishers/google/models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-2.5-flash-lite"  # confirmed on Maps-grounding supported-model list as of mid-2026
AUTH_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# A handful of major Indian metros as a convenience default for --cities.
# lat/lng are city-center approximations, fine for this purpose (Maps
# grounding uses them as a rough anchor, not a precise radius filter).
KNOWN_CITY_COORDS = {
    "bengaluru": (12.9716, 77.5946),
    "bangalore": (12.9716, 77.5946),
    "chennai": (13.0827, 80.2707),
    "hyderabad": (17.3850, 78.4867),
    "pune": (18.5204, 73.8567),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.7041, 77.1025),
    "kolkata": (22.5726, 88.3639),
    "ahmedabad": (23.0225, 72.5714),
    "jaipur": (26.9124, 75.7873),
    "howrah": (22.5958, 88.2636),
    "lucknow": (26.8467, 80.9462),
    "surat": (21.1702, 72.8311),
    "indore": (22.7196, 75.8577),
}


def _synthetic_key(name: str, location: str) -> str:
    """Stable fallback identifier for businesses with no real website, so
    sheets_sync.py's domain-based dedup doesn't collapse every no-website
    row into a single duplicate. Visually distinguishable from a real
    domain (no dot-tld shape) so it's obvious downstream this isn't a
    real site — but still unique per (name, location) pair."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{name}-{location}".lower()).strip("-")
    return f"no-website--{slug}"


# Same validator as gemini_search_finder.py, kept in sync: rejects
# explanatory text Gemini sometimes puts in the domain field (e.g. "Not
# explicitly found in search results") instead of leaving it empty, which
# would otherwise pass merge_prospects.py's domain-dedup as if it were a
# real, shared domain and silently drop real leads.
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.IGNORECASE)


def _looks_like_email(v: str) -> bool:
    """Basic shape check only — catches explanatory text or malformed
    output ('not listed', 'N/A', etc.) that isn't a real email address.
    Does NOT verify the address actually exists or is genuinely real —
    that's on the model following the 'don't guess' instruction, this is
    just a shape sanity check."""
    return bool(_EMAIL_RE.match((v or "").strip()))


def _looks_like_domain(value: str) -> bool:
    v = value.strip().lower()
    if not v or " " in v or "." not in v:
        return False
    v = v.removeprefix("http://").removeprefix("https://").removeprefix("www.")
    v = v.split("/", 1)[0]
    return bool(_DOMAIN_RE.match(v))


@dataclass
class FoundCompany:
    name: str
    domain: str
    reasoning: str
    location: str = ""
    address: str = ""
    category: str = ""
    phone: str = ""
    email: str = ""


EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "companies": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "domain": {"type": "STRING", "description": "website domain, no https://, e.g. example.com — empty string if genuinely none listed"},
                    "phone": {"type": "STRING", "description": "phone number exactly as listed on Google Maps, empty string if genuinely none listed — do not guess or reformat"},
                    "email": {"type": "STRING", "description": "email address exactly as listed on Google Maps or the business's own website, empty string if genuinely none listed — do not guess, construct, or infer one from the domain"},
                    "address": {"type": "STRING"},
                    "category": {"type": "STRING"},
                    "reasoning": {"type": "STRING", "description": "1 sentence on why this business fits the criteria"},
                },
                "required": ["name", "domain", "phone", "email", "address", "reasoning"],
            },
        }
    },
    "required": ["companies"],
}


class VertexGeminiClient:
    def __init__(self, project: str, model: str = DEFAULT_MODEL, location: str = LOCATION):
        self.project = project
        self.model = model
        self.location = location
        self.endpoint = VERTEX_ENDPOINT_TEMPLATE.format(location=location, project=project, model=model)
        try:
            self.credentials, _ = google.auth.default(scopes=AUTH_SCOPES)
        except Exception as e:
            raise ValueError(
                "Could not find Application Default Credentials. Run "
                "'gcloud auth application-default login' first, then retry.\n"
                f"(underlying error: {e})"
            )

    def _access_token(self) -> str:
        request = google.auth.transport.requests.Request()
        self.credentials.refresh(request)
        return self.credentials.token

    def _post(self, payload: dict, max_retries: int = 3) -> dict:
        last_err = None
        for attempt in range(max_retries):
            try:
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._access_token()}",
                }
                resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=60)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                print(f"  [warn] Vertex AI call failed ({e}); retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
        raise RuntimeError(f"Vertex AI call failed after {max_retries} attempts: {last_err}")

    def search_maps_for_companies(self, description: str, count: int, lat: float, lng: float, city_label: str) -> str:
        """Grounded call using the googleMaps tool, anchored to a specific
        city's lat/lng. Returns raw text (Gemini describing real listings
        it found via Maps grounding)."""
        prompt = (
            f"Using Google Maps data, find {count} real, small local businesses "
            f"in and around {city_label} that match this description: {description}\n\n"
            "IMPORTANT: only include genuinely small, lesser-known local businesses "
            "— NOT large chains, well-known brands, or major enterprise/SaaS vendors. "
            "Exclude anything that is a household name or a publicly-traded company.\n\n"
            "ALSO IMPORTANT: exclude AI companies, AI automation vendors, AI "
            "consultancies, or any business whose own product/service is AI or "
            "software automation — these are competitors or peers, not customers.\n\n"
            "For each business, state its name, its website (if listed — leave blank "
            "if Maps genuinely has no website for it, do not guess one), its phone "
            "number exactly as shown on its Google Maps listing (leave blank if none "
            "listed — do not guess or construct one), its email address if one is "
            "genuinely listed on its Maps profile or its own website (leave blank if "
            "none listed — do NOT construct one like info@ or contact@ from the "
            "domain, that is a fabrication, not a real finding), its address, its "
            "category, and one sentence on why it matches.\n\n"
            "CRITICAL RULE, NON-NEGOTIABLE: every business you list must be a real "
            "listing you found via Google Maps grounding — never invent, guess, or "
            f"fabricate one. If you cannot find {count} real matches in this area, "
            "list fewer and stop there. Returning 4 real listings is correct and "
            "useful; returning 8 invented ones is not, and will cause real harm "
            "since these are used to contact real businesses."
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"googleMaps": {}}],
            "toolConfig": {
                "retrievalConfig": {
                    "latLng": {"latitude": lat, "longitude": lng}
                }
            },
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048},
        }
        data = self._post(payload)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected response shape from Maps-grounded call: {json.dumps(data)[:500]}") from e

    def extract_companies(self, search_text: str, city_label: str) -> list[FoundCompany]:
        """Non-grounded call: reshapes the free-text Maps summary into
        strict JSON via responseSchema enforcement."""
        prompt = (
            "Extract the businesses mentioned in this text into structured data. "
            "If the text says it could NOT find real businesses, or presents "
            "examples explicitly labeled 'hypothetical', 'example', 'illustrative', "
            "or similar — return an EMPTY companies list. Do not extract fabricated "
            f"examples as if they were real results.\n\nText:\n\n{search_text}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 2048,
                "responseMimeType": "application/json",
                "responseSchema": EXTRACTION_SCHEMA,
            },
        }
        data = self._post(payload)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)

        placeholder_domains = {"example.com", "example.org", "yourcompany.com", "companyname.com"}
        hallucination_markers = ("hypothetical", "(example)", "illustrative", "fictional", "n/a", "unknown")

        real_candidates = []
        rejected = 0
        junk_domain_count = 0
        for c in parsed.get("companies", []):
            name = c.get("name", "")
            domain = (c.get("domain") or "").lower().strip()
            name_lower = name.lower()
            if domain in placeholder_domains:
                rejected += 1
                continue
            if any(marker in name_lower for marker in hallucination_markers):
                rejected += 1
                continue
            domain_out = c.get("domain", "")
            if domain and not _looks_like_domain(domain):
                # Explanatory text instead of a real domain or an empty
                # string — clear it so it falls back to _synthetic_key at
                # write time instead of risking a false dedup match against
                # an unrelated business with the same explanatory text.
                junk_domain_count += 1
                domain_out = ""
            email_out = (c.get("email") or "").strip()
            if email_out and not _looks_like_email(email_out):
                email_out = ""

            real_candidates.append(FoundCompany(
                name=name,
                domain=domain_out,
                location=city_label,
                address=c.get("address", ""),
                category=c.get("category", ""),
                phone=c.get("phone", ""),
                email=email_out,
                reasoning=c.get("reasoning", ""),
            ))

        if rejected:
            print(f"  [warn] Filtered out {rejected} fabricated/placeholder result(s) in {city_label}.", file=sys.stderr)
        if junk_domain_count:
            print(f"  [info] {junk_domain_count} result(s) in {city_label} had non-domain text "
                  f"in the domain field — cleared and given a synthetic dedupe key instead.", file=sys.stderr)

        return real_candidates


def parse_anchors(cities_arg: Optional[str], anchors_arg: Optional[str]) -> list[tuple[float, float, str]]:
    """Returns list of (lat, lng, label). --anchors takes precedence if both given."""
    anchors: list[tuple[float, float, str]] = []
    if anchors_arg:
        for chunk in anchors_arg.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            coords, _, label = chunk.partition(":")
            lat_str, _, lng_str = coords.partition(",")
            anchors.append((float(lat_str), float(lng_str), label.strip() or coords))
        return anchors

    if cities_arg:
        for city in cities_arg.split(","):
            city = city.strip()
            key = city.lower()
            if key not in KNOWN_CITY_COORDS:
                raise ValueError(
                    f"Unknown city '{city}' — not in the built-in list "
                    f"({', '.join(sorted(set(KNOWN_CITY_COORDS)))}). "
                    "Use --anchors 'lat,lng:Label' instead for custom locations."
                )
            lat, lng = KNOWN_CITY_COORDS[key]
            anchors.append((lat, lng, city))
        return anchors

    raise ValueError("Must supply either --cities or --anchors")


def run(out_csv: str, project: str, description: str, count_per_city: int,
        anchors: list[tuple[float, float, str]], model: str = DEFAULT_MODEL) -> list[FoundCompany]:
    client = VertexGeminiClient(project=project, model=model)
    all_companies: list[FoundCompany] = []

    for lat, lng, label in anchors:
        print(f"Searching Google Maps around {label} ({lat}, {lng})...")
        try:
            search_text = client.search_maps_for_companies(description, count_per_city, lat, lng, label)
            print(f"  [debug] Raw Maps text ({len(search_text)} chars):\n{search_text[:1500]}\n", file=sys.stderr)
            companies = client.extract_companies(search_text, label)
            print(f"  Found {len(companies)} businesses in {label}.")
            all_companies.extend(companies)
        except Exception as e:
            print(f"  [error] {label} failed: {e}", file=sys.stderr)
        time.sleep(0.5)

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Same column shape as icp_finder.py / gemini_search_finder.py's prospects.csv,
        # plus "email" (added this session, see gemini_search_finder.py's matching
        # empty column and merge_prospects.py's column-union for why this is safe
        # to add without breaking anything downstream) so downstream scripts work
        # with a real captured email, not just a guessed one.
        writer.writerow(["name", "domain", "phone", "email", "fit_score", "industry",
                          "employee_count", "location", "matched_signals", "source_url"])
        for c in all_companies:
            domain_out = c.domain.strip() if c.domain.strip() else _synthetic_key(c.name, c.location)
            writer.writerow([c.name, domain_out, c.phone, c.email, "", c.category, "", c.location, c.reasoning, ""])

    return all_companies


def main() -> None:
    parser = argparse.ArgumentParser(description="Find companies via Gemini + Google Maps grounding on Vertex AI.")
    parser.add_argument("--out", dest="out_csv", default="prospects.csv")
    parser.add_argument("--project", required=True, help="Google Cloud project ID")
    parser.add_argument("--description", required=True,
                         help="Free-text description of the businesses you're looking for")
    parser.add_argument("--cities", help="Comma-separated known city names, e.g. 'Bengaluru,Chennai'")
    parser.add_argument("--anchors", help="Custom anchors: 'lat,lng:Label;lat,lng:Label'")
    parser.add_argument("--count-per-city", type=int, default=8, help="Approx businesses to find per city anchor")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model id")
    args = parser.parse_args()

    anchors = parse_anchors(args.cities, args.anchors)
    # Pre-flight: each anchor costs 2 Vertex AI calls (1 grounded Maps search +
    # 1 non-grounded extraction). Printed before any billed call fires, so a
    # typo'd --cities list doesn't silently burn quota on the wrong scope.
    total_calls = len(anchors) * 2
    print(f"Plan: {len(anchors)} city anchor(s) -> {total_calls} Vertex AI calls "
          f"({len(anchors)} grounded Maps searches + {len(anchors)} extraction calls). "
          f"Free tier covers up to 1,500 grounded requests/day.\n")

    companies = run(args.out_csv, args.project, args.description, args.count_per_city, anchors, args.model)

    for c in companies[:15]:
        domain_note = c.domain if c.domain else "(no website listed)"
        phone_note = c.phone if c.phone else "no phone"
        print(f"  {c.name}  ({domain_note}, {phone_note}) — {c.location}")
    print(f"\nWrote {len(companies)} total rows to {args.out_csv}")


if __name__ == "__main__":
    main()
