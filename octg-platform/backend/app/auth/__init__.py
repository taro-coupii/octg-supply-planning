"""Authentication and authorization.

Layout:
    passwords.py   PBKDF2 hashing for the DEV credential store (stdlib only)
    tokens.py      HMAC-signed bearer tokens (stdlib only; no JWT dependency)
    provider.py    the pluggable "who are you" step -- dev now, Entra ID later
    deps.py        FastAPI dependencies: get_current_user + scope enforcement

The seam for Entra ID is `provider.py`: swapping providers changes how a
credential becomes a `User` row, and nothing else. Tokens, dependencies and the
scoping rules all operate on the row, not on the credential.
"""
