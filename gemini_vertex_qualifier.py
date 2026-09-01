#!/usr/bin/env python3
"""
gemini_vertex_qualifier.py — LLM-powered lead qualification + outreach
drafting, using Gemini via Vertex AI (Google Cloud) — bills against your
Google Cloud credit balance, unlike the AI Studio free-tier version
(gemini_qualifier.py, kept as a working fallback if you ever want the
simpler API-key auth instead).

Uses Gemini 3.1 Flash-Lite by default: cheap ($0.25/$1.50 per million tokens)
and fast, good for running this pipeline continuously without burning
through credit quickly. Upgrade to Gemini 3.1 Pro later (stronger reasoning,
~8x pricier) once the pipeline is proven working — no code change needed,
just pass --model gemini-3.1-pro on the command line.

This sits AFTER icp_finder.py in the pipeline, same shape as nvidia_qualifier.py:

    icp_finder.py  --------->  candidates (rule-based score)
                                     |
                                     v
    gemini_vertex_qualifier.py -->  qualification verdict + reasoning + draft outreach
                                     |
                                     v
    sheets_sync.py  ---------> Google Sheet

Setup (Application Default Credentials — the only auth method available on
Google Cloud orgs that disable both service-account key creation AND API
keys, as some do by default for security. ADC authenticates as your actual
Google account via the gcloud CLI, rather than a static key or key file):
    1. Install Google Cloud CLI: https://cloud.google.com/sdk/docs/install
    2. Run: gcloud auth application-default login
       (opens a browser, log in, grant permission — this saves credentials
       locally that any script on this machine can then use automatically)
    3. Run: gcloud config set project YOUR_PROJECT_ID
    4. In Cloud Console, confirm the "Vertex AI API" (now called "Agent
       Platform API") is enabled on that project.
    5. pip install requests google-auth

Usage:
    python gemini_vertex_qualifier.py --in prospects.csv --out qualified.csv \
        --project YOUR_PROJECT_ID \
        --product "your app name" --pitch "one-line description of what it does"
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests
import google.auth
import google.auth.transport.requests

# ADC uses OAuth2 bearer tokens, which only the project/location-scoped
# endpoint accepts (the global endpoint is for plain API keys, which this
# org's policy disallows — see setup notes above). us-central1 is Google's
# default/most-available region for Gemini on this platform.
LOCATION = "us-central1"
VERTEX_ENDPOINT_TEMPLATE = (
    "https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
    "/locations/{location}/publishers/google/models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-2.5-flash-lite"  # confirmed real/live Vertex AI model ID as of mid-2026; cheap, good for high-volume tasks
AUTH_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


@dataclass
class QualificationResult:
    domain: str
    name: str
    phone: str
    qualified: bool
    confidence: float          # 0-1, model's self-reported confidence
    reasoning: str
    pain_points_guess: str
    outreach_draft: str


# JSON schema passed to Gemini's responseSchema — this enforces valid JSON output
# structurally (Gemini refuses to emit anything that doesn't match), rather than
# relying on the model to follow a "respond only with JSON" instruction in the
# prompt. Same schema-enforcement mechanism as the AI Studio version; Vertex AI
# exposes the identical generationConfig fields since it serves the same models.
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "qualified": {"type": "BOOLEAN"},
        "confidence": {"type": "NUMBER"},
        "reasoning": {"type": "STRING"},
        "pain_points_guess": {"type": "STRING"},
        "outreach_draft": {"type": "STRING"},
    },
    "required": ["qualified", "confidence", "reasoning", "pain_points_guess", "outreach_draft"],
}

QUALIFY_SYSTEM_PROMPT = """You are a B2B sales qualification analyst. Given a company's \
name, industry, size, and description, and a description of the product being sold, \
you judge whether this company is a good sales target and draft a short, specific \
first-touch outreach note.

Rules for the outreach_draft: be warm and specific, reference something concrete \
about their business — but never disguise it as personally handwritten or claim a \
relationship that doesn't exist. It's fine for it to read as coming from a real \
person doing genuine research on this company; it must not falsely imply no \
automation was involved anywhere in this process."""


class VertexGeminiClient:
    def __init__(self, project: str, model: str = DEFAULT_MODEL, location: str = LOCATION):
        self.project = project
        self.model = model
        self.location = location
        self.endpoint = VERTEX_ENDPOINT_TEMPLATE.format(location=location, project=project, model=model)

        # google.auth.default() auto-discovers ADC credentials saved locally by
        # `gcloud auth application-default login` — no key file to load or manage.
        # This is the pattern from Google's own official Vertex AI auth docs.
        try:
            self.credentials, _ = google.auth.default(scopes=AUTH_SCOPES)
        except Exception as e:
            raise ValueError(
                "Could not find Application Default Credentials. Run "
                "'gcloud auth application-default login' first, then retry.\n"
                f"(underlying error: {e})"
            )

    def _access_token(self) -> str:
        # Tokens expire (~1hr); refresh() before every request is cheap and
        # avoids having to track expiry ourselves.
        request = google.auth.transport.requests.Request()
        self.credentials.refresh(request)
        return self.credentials.token

    def generate_json(self, system: str, user: str, schema: dict,
                       temperature: float = 0.3, max_retries: int = 3) -> dict:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 700,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }

        last_err = None
        for attempt in range(max_retries):
            try:
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._access_token()}",
                }
                resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except Exception as e:
                last_err = e
                wait = 2 ** attempt
                print(f"  [warn] Vertex AI call failed ({e}); retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
        raise RuntimeError(f"Vertex AI call failed after {max_retries} attempts: {last_err}")


def qualify_candidate(client: VertexGeminiClient, row: dict, product: str, pitch: str) -> QualificationResult:
    user_prompt = f"""Product being sold: {product}
What it does: {pitch}

Company to evaluate:
  Name: {row.get('name', '')}
  Domain: {row.get('domain', '')}
  Industry: {row.get('industry', '')}
  Employee count: {row.get('employee_count', '')}
  Location: {row.get('location', '')}
  Rule-based fit score: {row.get('fit_score', '')}
  Matched signals: {row.get('matched_signals', '')}
"""
    parsed = client.generate_json(QUALIFY_SYSTEM_PROMPT, user_prompt, RESPONSE_SCHEMA)

    return QualificationResult(
        domain=row.get("domain", ""),
        name=row.get("name", ""),
        phone=row.get("phone", ""),
        qualified=bool(parsed.get("qualified", False)),
        confidence=float(parsed.get("confidence", 0.0)),
        reasoning=parsed.get("reasoning", ""),
        pain_points_guess=parsed.get("pain_points_guess", ""),
        outreach_draft=parsed.get("outreach_draft", ""),
    )


def run(in_csv: str, out_csv: str, product: str, pitch: str, project: str,
        model: str = DEFAULT_MODEL) -> list[QualificationResult]:
    client = VertexGeminiClient(project=project, model=model)
    results: list[QualificationResult] = []

    with open(in_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"Qualifying {len(rows)} candidates via Vertex AI Gemini ({model})...")
    for i, row in enumerate(rows, 1):
        try:
            result = qualify_candidate(client, row, product, pitch)
            results.append(result)
            status = "QUALIFIED" if result.qualified else "skip"
            print(f"  [{i}/{len(rows)}] {result.name:30s} {status} (conf {result.confidence:.2f})")
        except Exception as e:
            print(f"  [{i}/{len(rows)}] {row.get('name', '?')} FAILED: {e}", file=sys.stderr)
        time.sleep(0.3)  # be a reasonable citizen re: Vertex AI rate limits

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "domain", "phone", "qualified", "confidence", "reasoning",
                          "pain_points_guess", "outreach_draft"])
        for r in results:
            writer.writerow([r.name, r.domain, r.phone, r.qualified, r.confidence,
                              r.reasoning, r.pain_points_guess, r.outreach_draft])

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Qualify ICP candidates via Gemini on Vertex AI (ADC auth).")
    parser.add_argument("--in", dest="in_csv", required=True, help="CSV from icp_finder.py")
    parser.add_argument("--out", dest="out_csv", default="qualified.csv")
    parser.add_argument("--project", required=True, help="Google Cloud project ID")
    parser.add_argument("--product", required=True, help="Your product/app name")
    parser.add_argument("--pitch", required=True, help="One-line description of what it does / solves")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model id")
    args = parser.parse_args()

    results = run(args.in_csv, args.out_csv, args.product, args.pitch, args.project, args.model)
    qualified_count = sum(1 for r in results if r.qualified)
    print(f"\nDone. {qualified_count}/{len(results)} qualified. Wrote {args.out_csv}")


if __name__ == "__main__":
    main()
