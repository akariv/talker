"""Provision an ESP32 client by fetching config from Firestore and writing secrets.h.

Usage: python scripts/provision.py <client_id>

Reads from Firestore:
  - config/wifi       → WIFI_SSID, WIFI_PASS
  - config/server     → SERVER_URL
  - clients/<id>      → CLIENT_NAME, CLIENT_API_KEY
"""

import os
import sys
import json
import subprocess

PROJECT_ID = "talker-491708"
SECRETS_PATH = os.path.join(os.path.dirname(__file__), "..", "client", "main", "secrets.h")


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
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <client_id>")
        sys.exit(1)

    client_id = sys.argv[1]
    print(f"Provisioning client: {client_id}")

    token = get_access_token()

    # Fetch config
    print("  Fetching WiFi config...")
    wifi = firestore_get(token, "config", "wifi")
    ssid = wifi["ssid"]
    password = wifi["password"]
    print(f"    SSID: {ssid}")

    print("  Fetching server config...")
    server = firestore_get(token, "config", "server")
    server_url = server["url"]
    print(f"    URL: {server_url}")

    print(f"  Fetching client '{client_id}'...")
    client = firestore_get(token, "clients", client_id)
    api_key = client["api_key"]
    print(f"    API key: {api_key[:8]}...")

    # Write secrets.h
    secrets_content = f'''#pragma once

#define WIFI_SSID       "{ssid}"
#define WIFI_PASS       "{password}"

#define SERVER_URL      "{server_url}"

#define CLIENT_NAME     "{client_id}"
#define CLIENT_API_KEY  "{api_key}"
'''

    with open(SECRETS_PATH, "w") as f:
        f.write(secrets_content)

    print(f"\n  Written to {os.path.abspath(SECRETS_PATH)}")
    print("  Done! Now run: just build or just fm")


if __name__ == "__main__":
    main()
