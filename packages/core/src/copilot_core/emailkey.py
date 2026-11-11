"""`uv run copilot-email-key`: a signing key for the local stack's email sign-in.

Prints the two lines for `.env`. This is for a machine of your own: a deployed stage's key is
made by its own tooling straight into the secret store and is never printed
(`infra/gcp/scripts/stage.py secrets generate`).
"""

from copilot_core.auth import new_signing_key


def run() -> None:
    signing_key, _ = new_signing_key()
    print("COPILOT_EMAIL_SIGN_IN=true")
    print(f"COPILOT_EMAIL_SIGNING_KEY={signing_key}")
