"""
Rule-based contract risk engine.

This is deliberately implemented as a transparent, explainable rule engine
rather than a black-box ML model: for a legal-risk tool, being able to say
*exactly* why a clause was flagged (which phrase triggered it) is more
valuable — and more trustworthy — than a opaque probability score. It also
keeps the project fully within the AWS Free Tier since no paid NLP
inference is required.

Each risk category has:
  - a set of trigger phrases/regex patterns (case-insensitive)
  - a severity level (LOW / MEDIUM / HIGH)
  - a base score contribution
  - a human-readable explanation template
"""

import re
from dataclasses import dataclass
from typing import List

SEVERITY_WEIGHTS = {"LOW": 10, "MEDIUM": 25, "HIGH": 45}


@dataclass
class RiskCategory:
    key: str
    label: str
    patterns: List[str]
    severity: str
    explanation: str


RISK_CATEGORIES: List[RiskCategory] = [
    RiskCategory(
        key="AUTO_RENEWAL",
        label="Automatic Renewal",
        patterns=[
            r"automatically renew",
            r"auto-renew",
            r"shall renew for successive",
            r"evergreen",
        ],
        severity="MEDIUM",
        explanation=(
            "This clause causes the contract to renew automatically unless "
            "explicitly cancelled, which can trap you into unwanted extra terms "
            "if you miss the cancellation window."
        ),
    ),
    RiskCategory(
        key="UNLIMITED_LIABILITY",
        label="Unlimited Liability",
        patterns=[
            r"unlimited liability",
            r"no limitation of liability",
            r"without limitation as to (?:amount|damages)",
            r"shall be liable for all damages",
        ],
        severity="HIGH",
        explanation=(
            "This clause does not cap your financial liability, meaning a single "
            "dispute could expose you to damages far exceeding the contract value."
        ),
    ),
    RiskCategory(
        key="ONE_SIDED_INDEMNIFICATION",
        label="One-Sided Indemnification",
        patterns=[
            r"indemnify and hold harmless",
            r"shall indemnify",
            r"defend, indemnify",
        ],
        severity="HIGH",
        explanation=(
            "This clause requires one party to cover the other party's losses, "
            "legal fees, or claims. Check whether this obligation is mutual or "
            "falls one-sidedly on you."
        ),
    ),
    RiskCategory(
        key="UNILATERAL_TERMINATION",
        label="Unilateral Termination Rights",
        patterns=[
            r"may terminate this agreement at any time for any reason",
            r"sole discretion.{0,40}terminate",
            r"terminate.{0,40}without cause",
        ],
        severity="MEDIUM",
        explanation=(
            "This clause allows one party to end the agreement at will, which "
            "creates uncertainty and reduces your ability to rely on the "
            "contract's term."
        ),
    ),
    RiskCategory(
        key="BROAD_NON_COMPETE",
        label="Broad Non-Compete",
        patterns=[
            r"shall not.{0,40}engage in any (?:similar|competing) business",
            r"non-compete",
            r"restrictive covenant",
        ],
        severity="MEDIUM",
        explanation=(
            "This clause restricts your ability to work with competitors or "
            "operate in the same industry. Check the geographic scope and "
            "duration — overly broad restrictions may also be unenforceable."
        ),
    ),
    RiskCategory(
        key="PERPETUAL_CONFIDENTIALITY",
        label="Perpetual / Unbounded Confidentiality",
        patterns=[
            r"in perpetuity",
            r"shall remain confidential indefinitely",
            r"no time limit",
        ],
        severity="LOW",
        explanation=(
            "This confidentiality obligation has no end date, which can create "
            "long-term compliance burden well after the business relationship ends."
        ),
    ),
    RiskCategory(
        key="ASSIGNMENT_WITHOUT_CONSENT",
        label="Assignment Without Consent",
        patterns=[
            r"may assign this agreement without.{0,20}consent",
            r"freely assign",
        ],
        severity="MEDIUM",
        explanation=(
            "This clause lets the other party transfer their rights/obligations "
            "under the contract to a third party without asking you first."
        ),
    ),
    RiskCategory(
        key="UNFAVORABLE_JURISDICTION",
        label="Unfavorable Governing Law / Jurisdiction",
        patterns=[
            r"governed by the laws of",
            r"exclusive jurisdiction",
            r"venue shall lie exclusively",
        ],
        severity="LOW",
        explanation=(
            "This clause fixes which region's laws and courts apply to any "
            "dispute. If that jurisdiction is far from you, resolving a dispute "
            "could become expensive and inconvenient."
        ),
    ),
    RiskCategory(
        key="LATE_PAYMENT_PENALTY",
        label="Late Payment Penalty",
        patterns=[
            r"late fee",
            r"penalty of \d+%",
            r"interest.{0,20}per (?:month|annum) on overdue",
        ],
        severity="LOW",
        explanation=(
            "This clause imposes financial penalties for late payment. Confirm "
            "the rate and whether it compounds, since aggressive terms can add "
            "up quickly."
        ),
    ),
    RiskCategory(
        key="IP_ASSIGNMENT",
        label="Broad Intellectual Property Assignment",
        patterns=[
            r"all right, title, and interest.{0,60}shall (?:vest|belong)",
            r"work made for hire",
            r"assigns all intellectual property",
        ],
        severity="MEDIUM",
        explanation=(
            "This clause transfers ownership of intellectual property created "
            "under the contract to the other party. Confirm this scope matches "
            "your expectations — overly broad IP assignment can affect unrelated "
            "prior work."
        ),
    ),
]

_COMPILED = [
    (cat, [re.compile(p, re.IGNORECASE) for p in cat.patterns]) for cat in RISK_CATEGORIES
]


def split_into_clauses(full_text: str) -> List[str]:
    """
    Splits contract text into clause-sized chunks. Contracts are typically
    structured into numbered clauses/sections or paragraphs; we split on
    blank lines and numbered-section markers, then discard trivially short
    fragments (headers, page numbers, whitespace artifacts from OCR).
    """
    # Normalize line endings and collapse excessive whitespace from OCR noise
    text = re.sub(r"\r\n?", "\n", full_text)
    text = re.sub(r"[ \t]+", " ", text)

    # Split on blank lines and on lines that look like "1.", "2.1", "Section 3:"
    raw_chunks = re.split(r"\n\s*\n|\n(?=\d+\.\d*\s)|\n(?=Section\s+\d+)", text)

    clauses = [c.strip() for c in raw_chunks if len(c.strip()) >= 40]
    return clauses


def score_clause(clause_text: str) -> List[dict]:
    """
    Returns a list of risk matches found within a single clause. A clause can
    match more than one category (e.g. a termination clause that also
    contains a liability waiver).
    """
    matches = []
    for category, compiled_patterns in _COMPILED:
        for pattern in compiled_patterns:
            if pattern.search(clause_text):
                matches.append(
                    {
                        "category": category.key,
                        "label": category.label,
                        "severity": category.severity,
                        "score": SEVERITY_WEIGHTS[category.severity],
                        "explanation": category.explanation,
                    }
                )
                break  # one match per category is enough for this clause
    return matches


def analyze_contract_text(full_text: str) -> dict:
    """
    Runs the full pipeline: split into clauses, score each clause, and
    aggregate into an overall risk score (0-100) and risk level.

    Returns:
        {
            "clauses": [ { clause_index, clause_text, category, severity, score, explanation }, ... ],
            "overall_score": float,
            "risk_level": "LOW" | "MEDIUM" | "HIGH",
        }
    """
    raw_clauses = split_into_clauses(full_text)

    flagged_clauses = []
    total_score = 0.0

    for index, clause_text in enumerate(raw_clauses):
        matches = score_clause(clause_text)
        for match in matches:
            flagged_clauses.append(
                {
                    "clause_index": index,
                    "clause_text": clause_text[:2000],  # guard against pathological OCR runs
                    "category": match["category"],
                    "severity": match["severity"],
                    "score": match["score"],
                    "explanation": match["explanation"],
                }
            )
            total_score += match["score"]

    # Normalize to a 0-100 scale; cap so a huge contract doesn't blow past 100
    overall_score = min(100.0, round(total_score, 2))

    if overall_score >= 60:
        risk_level = "HIGH"
    elif overall_score >= 25:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return {
        "clauses": flagged_clauses,
        "overall_score": overall_score,
        "risk_level": risk_level,
        "total_clauses_scanned": len(raw_clauses),
    }
