"""Provision an ESP32 client by fetching config from Firestore and writing secrets.h.

Usage: python scripts/provision.py [--project <name>] <client_id>

Reads from Firestore:
  - config/wifi       → WIFI_SSID, WIFI_PASS
  - config/server     → SERVER_URL
  - clients/<id>      → CLIENT_NAME, CLIENT_API_KEY

Writes to: firmware/projects/<project>/main/secrets.h
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ID = "talker-491708"
REPO_ROOT = Path(__file__).resolve().parent.parent


def get_access_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def firestore_get(token: str, collection: str, doc_id: str) -> dict:
    """Fetch a Firestore document, return its fields as a simple dict."""
    import urllib.request
    url = (
        f"https://firestore.googleapis.com/v1/"
        f"projects/{PROJECT_ID}/databases/(default)/documents/{collection}/{doc_id}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    # Convert Firestore field format to simple key-value
    fields = data.get("fields", {})
    result = {}
    for key, val in fields.items():
        for type_key in ("stringValue", "integerValue", "doubleValue", "booleanValue"):
            if type_key in val:
                result[key] = val[type_key]
                break
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default="talker",
                        help="Firmware project under firmware/projects/ (default: talker)")
    parser.add_argument("client_id", help="Firestore client doc ID")
    args = parser.parse_args()

    project_dir = REPO_ROOT / "firmware" / "projects" / args.project
    if not project_dir.is_dir():
        print(f"error: no such firmware project: {project_dir}", file=sys.stderr)
        sys.exit(1)
    secrets_path = project_dir / "main" / "secrets.h"

    print(f"Provisioning client '{args.client_id}' for project '{args.project}'")

    token = get_access_token()

    print("  Fetching WiFi config...")
    wifi = firestore_get(token, "config", "wifi")
    ssid = wifi["ssid"]
    password = wifi["password"]
    print(f"    SSID: {ssid}")

    print("  Fetching server config...")
    server = firestore_get(token, "config", "server")
    server_url = server["url"]
    print(f"    URL: {server_url}")

    print(f"  Fetching client '{args.client_id}'...")
    client = firestore_get(token, "clients", args.client_id)
    api_key = client["api_key"]
    print(f"    API key: {api_key[:8]}...")

    secrets_content = f'''#pragma once

#define WIFI_SSID       "{ssid}"
#define WIFI_PASS       "{password}"

#define SERVER_URL      "{server_url}"

#define CLIENT_NAME     "{args.client_id}"
#define CLIENT_API_KEY  "{api_key}"
'''

    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(secrets_content)

    print(f"\n  Written to {secrets_path}")
    print(f"  Done! Now run: just PROJECT={args.project} build  (or: just fm)")


if __name__ == "__main__":
    main()
