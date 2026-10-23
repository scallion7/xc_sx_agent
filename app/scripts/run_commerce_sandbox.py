"""Run the independent HTTP simulator. Credentials and callback destination are server config."""

import os
from pathlib import Path

import uvicorn
from app.sandbox.api import create_sandbox_app


def application():
    url = os.environ.get(
        "SANDBOX_DATABASE_URL", "sqlite:///app/sessions/commerce-sandbox.db"
    )
    application_url = os.environ.get(
        "DATABASE_URL", "sqlite:///app/sessions/xc_sx_agent.db"
    )

    def canonical(value):
        return (
            str(Path(value[10:]).resolve()) if value.startswith("sqlite:///") else value
        )

    if canonical(url) == canonical(application_url):
        raise ValueError("Sandbox must use a separate database")
    if url.startswith("sqlite:///"):
        Path(url[10:]).parent.mkdir(parents=True, exist_ok=True)
    return create_sandbox_app(
        url,
        token=os.environ.get("SANDBOX_API_TOKEN", "sandbox-dev-key"),
        admin_token=os.environ.get("SANDBOX_ADMIN_TOKEN", "sandbox-dev-admin"),
        callback_url=os.environ.get("SANDBOX_CALLBACK_URL", ""),
        callback_secret=os.environ.get("PROVIDER_WEBHOOK_SECRET", ""),
        supports_reversal=os.environ.get("SANDBOX_SUPPORTS_REVERSAL", "false").lower()
        == "true",
    )


def main():
    uvicorn.run(
        application(),
        host=os.environ.get("SANDBOX_HOST", "127.0.0.1"),
        port=int(os.environ.get("SANDBOX_PORT", "9124")),
    )


if __name__ == "__main__":
    main()
