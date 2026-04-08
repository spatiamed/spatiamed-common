import re
from dataclasses import dataclass


@dataclass
class NMCViolation:
    term: str
    rule: str
    severity: str     # "block" = cannot send, "warn" = flag for review
    suggestion: str


NMC_RULES = {
    "superlative_claims": {
        "severity": "block",
        "patterns": [
            r"\bbest\b", r"\b#1\b", r"\bnumber\s*one\b", r"\btop\s*(rated|ranked)\b",
            r"\bleading\b", r"\bmost\s+advanced\b", r"\bworld.?class\b",
        ],
        "suggestion": "Remove superlative. Use: 'experienced', 'qualified', 'specialized in'.",
    },
    "guaranteed_outcomes": {
        "severity": "block",
        "patterns": [
            r"\bguarantee[ds]?\b", r"\b100%", r"\bcure[ds]?\b",
            r"\bpermanent\s*(solution|cure|fix)\b", r"\brisk.?free\b",
        ],
        "suggestion": "Remove outcome guarantee. Use: 'treatment options', 'care plan'.",
    },
    "misleading_testimonials": {
        "severity": "block",
        "patterns": [
            r"\bpatient\s+says?\b.*\b(cured|healed|saved)\b",
            r"\btestimonial\b", r"\bsuccess\s+stor(y|ies)\b",
        ],
        "suggestion": "Patient testimonials with outcome claims are prohibited.",
    },
    "pricing_inducement": {
        "severity": "warn",
        "patterns": [
            r"\bfree\s+(surgery|treatment|consultation)\b",
            r"\bdiscount\b.*\b(surgery|treatment|procedure)\b",
        ],
        "suggestion": "Avoid price-based inducements for medical services.",
    },
}


def check_content(text: str) -> list[NMCViolation]:
    """Check text against NMC advertising rules.

    Returns empty list if compliant.
    Any violation with severity="block" means content MUST NOT be sent.
    """
    violations = []
    for rule_name, rule in NMC_RULES.items():
        for pattern in rule["patterns"]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                violations.append(NMCViolation(
                    term=match.group(),
                    rule=rule_name,
                    severity=rule["severity"],
                    suggestion=rule["suggestion"],
                ))
    return violations


def has_blocking_violation(violations: list[NMCViolation]) -> bool:
    """Returns True if any violation has severity='block'."""
    return any(v.severity == "block" for v in violations)
