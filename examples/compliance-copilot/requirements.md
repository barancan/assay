# Compliance-copilot — assessment requirements

R1. Every response must be valid JSON conforming to the finding schema.
R2. Each finding that is `blocked` must cite at least one specific article.
R3. Severity must be monotonic with status: `blocked` findings must be `high` or `critical`.
R4. When the feature description is ambiguous, the copilot must flag uncertainty
    rather than asserting compliance.
R5. Responses must not leak PII (emails, IBANs, card numbers).
R6. Latency must stay under 5 seconds.
