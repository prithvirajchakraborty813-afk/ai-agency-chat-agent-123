#!/usr/bin/env python3
"""
gemini_search_finder.py — Finds candidate companies using Gemini's
"Grounding with Google Search" tool on Vertex AI, instead of a company
database like PDL/Apollo. Built as a fallback while PDL's free-tier
company-search quota is exhausted (resets monthly) — this doesn't touch
PDL at all, so it's usable immediately.

Why this works: instead of querying a pre-built company database, this
asks Gemini to use live Google Search to find companies matching your
criteria, then extracts structured results (name, domain, why it fits)
from what it finds. Real web search under the hood, via Google's own
"Grounding with Google Search" tool — NOT the deprecated Custom Search
JSON API (that one is closed to new customers; this is a different,
current product).

Billing: uses your Google Cloud credit via Vertex AI (same ADC auth as
gemini_vertex_qualifier.py). Vertex AI gives 1,500 free grounded requests
per day (up to 45,000/month) before any charge — each run of this script
is one grounded request, so this is effectively free at the volumes this
project needs.

This can slot into the pipeline in place of icp_finder.py's PDL source:

    gemini_search_finder.py  --->  prospects.csv        (find candidates)
                                          |
                                          v
    gemini_vertex_qualifier.py  --->  qualified.csv      (score + draft outreach)
                                          |
                                          v
    sheets_sync.py  ---------> Google Sheet

Setup: same as gemini_vertex_qualifier.py —
    1. gcloud auth application-default login
    2. gcloud config set project YOUR_PROJECT_ID
    3. pip install requests google-auth

Usage:
    python gemini_search_finder.py --out prospects.csv \
        --project YOUR_PROJECT_ID \
        --description "small businesses (2-50 employees) in India that would benefit from AI automation" \
        --count 20
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
import google.auth
import google.auth.transport.requests

LOCATION = "us-central1"
VERTEX_ENDPOINT_TEMPLATE = (
    "https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/{location}/publishers/google/models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-2.5-flash-lite"  # confirmed real/live Vertex AI model ID as of mid-2026; cheap, good for high-volume tasks
AUTH_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _synthetic_key(name: str, location: str) -> str:
    """Stable fallback identifier for companies with no real domain found,
    so sheets_sync.py's domain-based dedup doesn't collapse every
    no-domain row into a single duplicate (an empty domain string is the
    same key for every such row, which silently drops all but the first
    from the synced sheet)."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{name}-{location}".lower()).strip("-")
    return f"no-domain--{slug}"


# Matches a bare domain shape: label(.label)+ with a plausible TLD, no
# spaces, no protocol. Deliberately strict rather than clever — this only
# needs to reject junk, not validate real-world registrability.
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _looks_like_domain(value: str) -> bool:
    """True only if `value` actually has the shape of a domain.

    Gemini's responseSchema enforces that the `domain` field is a STRING,
    but not that it's a real domain — in practice it sometimes fills the
    field with explanatory text instead of leaving it empty, e.g. "Not
    explicitly found in search results". That text is non-empty, so the
    old `if not domain_lower` check let it through untouched. Multiple
    unrelated companies can get the *same* explanatory string, and
    merge_prospects.py dedupes on domain — so those companies silently
    collapse into "duplicates" and get dropped. Confirmed against real
    output: 8 companies in one run shared that exact phrase as their
    "domain". Anything that doesn't look like an actual domain is now
    treated as "no domain" and routed through _synthetic_key instead."""
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
    source_url: str = ""


# Schema for the structured extraction pass (see run() for why this is two
# calls: one grounded search call to gather real information, one
# non-grounded call to reliably reshape that into structured JSON — Gemini's
# responseSchema enforcement doesn't currently combine with the google_search
# tool in the same call, so this two-step split is a real necessity, not
# extra complexity for its own sake).
EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "companies": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "domain": {"type": "STRING", "description": "company website domain, no https://, e.g. example.com"},
                    "location": {"type": "STRING"},
                    "reasoning": {"type": "STRING", "description": "1 sentence on why this company fits the criteria"},
                },
                "required": ["name", "domain", "reasoning"],
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

    def search_for_companies(self, description: str, count: int) -> str:
        """Grounded call: asks Gemini to search the web and describe matching
        companies in free text. Returns the raw text response (with citations
        Gemini found), which extract_companies() then structures."""
        prompt = (
            f"Search the web and find {count} real companies that match this description: "
            f"{description}\n\n"
            "IMPORTANT: only include genuinely small, lesser-known local businesses "
            "— NOT large, well-known companies. Explicitly exclude any company that "
            "is a household name, a major enterprise software vendor (e.g. Zoho, "
            "Salesforce, HubSpot, Canva, or similar large SaaS/tech companies), a "
            "publicly-traded company, or a company with more than roughly 50 "
            "employees. Prioritize smaller, local, less-famous businesses even if "
            "they're harder to find — that's the actual target, not brand "
            "recognition.\n\n"
            "ALSO IMPORTANT: exclude AI companies, AI automation vendors, AI "
            "consultancies, or any business whose own product/service is AI or "
            "software automation — these are competitors or peers, not customers. "
            "The target is ordinary small businesses in non-tech industries "
            "(restaurants, retail, local services, clinics, manufacturers, etc.).\n\n"
            "If a search only surfaces large well-known companies or AI/tech "
            "vendors for part of the description, keep searching for smaller, "
            "non-tech alternatives rather than including them as filler.\n\n"
            "For each company, state its name, its website domain, its approximate "
            "location, and one sentence on why it matches. List them clearly, one "
            "per line or paragraph.\n\n"
            "CRITICAL RULE, NON-NEGOTIABLE: every company you list MUST be a real, "
            "actual business you found genuine evidence of via search — a real name, "
            "a real findable website or listing. NEVER invent, guess, or fabricate "
            "a plausible-sounding company, and NEVER label something 'hypothetical' "
            "or 'example' and include it anyway. If you cannot find enough real "
            f"companies to reach {count}, return fewer — for example, if you can "
            "only verify 6 real companies, list exactly those 6 and stop there. "
            "Returning 8 real companies is correct and useful; returning 20 "
            "invented ones is not, and will cause real harm since these are used "
            "to contact real businesses. If you are not confident a company is "
            "real, leave it out entirely rather than including it with a caveat."
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"googleSearch": {}}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048},
        }
        data = self._post(payload)
        candidates = data.get("candidates") or []
        if not candidates:
            print(f"  [warn] Gemini returned no candidates. Full response: {data}", file=sys.stderr)
            return ""
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason", "UNKNOWN")
        parts = candidate.get("content", {}).get("parts") or []
        if not parts:
            print(
                f"  [warn] Gemini candidate had no 'parts' (finishReason={finish_reason}). "
                f"Full candidate: {candidate}",
                file=sys.stderr,
            )
            return ""
        # Concatenate all text parts, in case the grounded response is split
        # across multiple parts (e.g. text interleaved with search metadata).
        text = "".join(p.get("text", "") for p in parts)
        if not text.strip():
            print(
                f"  [warn] Gemini candidate had parts but no usable text (finishReason={finish_reason}).",
                file=sys.stderr,
            )
        return text

    def extract_companies(self, search_text: str) -> list[FoundCompany]:
        """Non-grounded call: reshapes the free-text search summary into
        strict JSON via responseSchema enforcement."""
        prompt = (
            "Extract the companies mentioned in this text into structured data. "
            "If the text says it could NOT find real companies, or presents "
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
        candidates = [
            FoundCompany(
                name=c.get("name", ""),
                domain=c.get("domain", ""),
                location=c.get("location", ""),
                reasoning=c.get("reasoning", ""),
            )
            for c in parsed.get("companies", [])
        ]

        # Hard code-level filter, independent of prompt instructions: the
        # prompt alone was not reliable enough in testing (Gemini once
        # returned 21 companies explicitly labeled "(Hypothetical)" in its
        # own text, using placeholder domains, despite being told not to).
        # This rejects the concrete, checkable markers of a fabricated
        # result rather than trusting the model's self-report.
        placeholder_domains = {"example.com", "example.org", "yourcompany.com", "companyname.com"}
        hallucination_markers = ("hypothetical", "(example)", "illustrative", "fictional", "n/a", "unknown")

        real_candidates = []
        no_domain_count = 0
        junk_domain_count = 0
        for c in candidates:
            domain_lower = c.domain.lower().strip()
            name_lower = c.name.lower()
            if domain_lower in placeholder_domains:
                continue
            if any(marker in name_lower for marker in hallucination_markers):
                continue
            # A missing domain alone is NOT treated as a fabrication signal —
            # a real small business can genuinely lack a findable website in
            # the search text. Rejecting these outright was silently
            # discarding real leads; they're kept and given a synthetic
            # dedupe key at write time instead (see _synthetic_key below).
            if not domain_lower:
                no_domain_count += 1
            elif not _looks_like_domain(domain_lower):
                # Gemini put explanatory text (e.g. "Not explicitly found in
                # search results") in the domain field instead of leaving it
                # empty. Treat it the same as a missing domain rather than
                # trusting it — see _looks_like_domain's docstring.
                junk_domain_count += 1
                c.domain = ""
            real_candidates.append(c)

        rejected = len(candidates) - len(real_candidates)
        if rejected:
            print(f"  [warn] Filtered out {rejected} fabricated/placeholder result(s) "
                  f"— these were not real, verifiable companies.", file=sys.stderr)
        if no_domain_count:
            print(f"  [info] {no_domain_count} kept result(s) had no domain in the search "
                  f"text — given a synthetic dedupe key on write instead of being dropped.", file=sys.stderr)
        if junk_domain_count:
            print(f"  [info] {junk_domain_count} kept result(s) had non-domain text (e.g. "
                  f"\"Not explicitly found...\") in the domain field — cleared and given a "
                  f"synthetic dedupe key instead of risking a false duplicate.", file=sys.stderr)

        return real_candidates


def run(out_csv: str, project: str, description: str, count: int, model: str = DEFAULT_MODEL) -> list[FoundCompany]:
    client = VertexGeminiClient(project=project, model=model)

    print(f"Searching the web for: {description}")
    search_text = client.search_for_companies(description, count)

    if not search_text.strip():
        print(
            "  [warn] Search grounding returned no usable text this run "
            "(see warnings above). Writing an empty result file instead of "
            "crashing the chain — the Maps finder's results can still be used.",
            file=sys.stderr,
        )
        companies: list[FoundCompany] = []
    else:
        print(f"  [debug] Raw search text ({len(search_text)} chars):\n{search_text[:2000]}\n", file=sys.stderr)
        print("  [debug] Extracting structured data from the above...", file=sys.stderr)
        companies = client.extract_companies(search_text)

    print(f"Found {len(companies)} companies.")

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Matches icp_finder.py's prospects.csv column shape where possible,
        # so downstream scripts (gemini_vertex_qualifier.py, sheets_sync.py)
        # work unmodified against either source.
        writer.writerow(["name", "domain", "phone", "fit_score", "industry", "employee_count",
                          "location", "matched_signals", "source_url"])
        for c in companies:
            domain_out = c.domain.strip() if c.domain.strip() else _synthetic_key(c.name, c.location)
            # Web-search grounding doesn't reliably surface phone numbers the way
            # Maps listings do — left blank here rather than guessed. Use
            # gemini_maps_finder.py for phone-number coverage.
            writer.writerow([c.name, domain_out, "", "", "", "", c.location, c.reasoning, ""])

    return companies


def main() -> None:
    parser = argparse.ArgumentParser(description="Find companies via Gemini + Google Search grounding on Vertex AI.")
    parser.add_argument("--out", dest="out_csv", default="prospects.csv")
    parser.add_argument("--project", required=True, help="Google Cloud project ID")
    parser.add_argument("--description", required=True,
                         help="Free-text description of the companies you're looking for")
    parser.add_argument("--count", type=int, default=20, help="Approximate number of companies to find")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model id")
    args = parser.parse_args()

    companies = run(args.out_csv, args.project, args.description, args.count, args.model)
    for c in companies[:10]:
        print(f"  {c.name}  ({c.domain}) — {c.location}")
    print(f"\nWrote {args.out_csv}")


if __name__ == "__main__":
    main()
