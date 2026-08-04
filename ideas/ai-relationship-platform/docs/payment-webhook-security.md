# Payment webhook security

Stripe verification receives the exact ASGI body bytes and `Stripe-Signature`; invalid,
missing, or body-mismatched signatures are rejected before persistence. Bodies are size
bounded. Only event/object identity and a SHA-256 payload digest are stored.

For YooKassa the ASGI peer is authoritative unless it belongs to
`YOOKASSA_TRUSTED_PROXY_ALLOWLIST`. Only then is exactly one `Forwarded` or
`X-Forwarded-For` chain parsed, right-to-left across trusted hops. Malformed, ambiguous,
or spoofed chains are rejected. The resolved source must belong to
`YOOKASSA_WEBHOOK_IP_ALLOWLIST`. IP validation permits inbox insertion only: workers
always retrieve the provider object before financial changes.

Duplicate IDs with the same digest are acknowledged. A changed digest sets
`manual_review` and emits an identifier-only security log. Secrets, payload bodies,
receipt contacts, and payment-method details must not be logged.
