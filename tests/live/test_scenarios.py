"""Live integration tests for multi-client intercom routing.

Sends voice messages to the production server, polls for responses,
and plays them back locally.

Usage:
    python generate_audio.py   # first time only — creates audio files
    python test_scenarios.py
"""

import os
import sys
import time
import wave
import subprocess
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

import requests

SERVER = "https://talker-server-zwmd6hvg7q-ez.a.run.app"

CLIENTS = {
    "kitchen": "Yi1uisae8ahc8ce6cooQu4aet6ZaiTho",
    "alice-room": "sae5Que6hupaic4ju1haa4pee3Ieyi5b",
    "bob-room": "Yeephei4EeShohVoo6ohtoh1deixei8d",
}

SCENARIOS = [
    {
        "name": "Kitchen announces dinner",
        "sender": "kitchen",
        "audio": "audio/dinner_ready.raw",
        "expect_responses_on": ["alice-room", "bob-room"],
        "description": "Kitchen says 'dinner is ready' → Alice and Bob should get messages",
    },
    {
        "name": "Alice asks a question",
        "sender": "alice-room",
        "audio": "audio/capital_of_france.raw",
        "expect_responses_on": ["alice-room"],
        "description": "Alice asks 'what is the capital of France?' → Alice should get 'Paris'",
    },
    {
        "name": "Bob relays to Alice",
        "sender": "bob-room",
        "audio": "audio/ask_alice_movies.raw",
        "expect_responses_on": ["alice-room"],
        "description": "Bob says 'ask Alice if she wants to go to the movies' → Alice should get the message",
    },
    {
        "name": "Alice replies to Bob",
        "sender": "alice-room",
        "audio": "audio/alice_sure.raw",
        "expect_responses_on": ["bob-room"],
        "description": "Alice says 'sure!' → Bob should hear that Alice agreed to the movies",
    },
]


def headers(client_name: str) -> dict:
    return {
        "X-Client-Name": client_name,
        "X-Api-Key": CLIENTS[client_name],
    }


def send_voice(client_name: str, audio_path: str):
    """Upload audio file to server using the chunked /reset → /upload → /done flow."""
    print(f"  Sending from {client_name}: {audio_path}")

    # Reset
    r = requests.post(f"{SERVER}/reset", headers=headers(client_name))
    assert r.status_code == 200, f"Reset failed: {r.status_code}"

    # Upload in chunks
    with open(audio_path, "rb") as f:
        data = f.read()

    chunk_size = 4096
    for i in range(0, len(data), chunk_size):
        chunk = data[i : i + chunk_size]
        r = requests.post(
            f"{SERVER}/upload",
            headers={**headers(client_name), "Content-Type": "application/octet-stream"},
            data=chunk,
        )
        assert r.status_code == 200, f"Upload failed: {r.status_code}"

    # Done
    r = requests.post(f"{SERVER}/done", headers=headers(client_name))
    assert r.status_code == 202, f"Done failed: {r.status_code} {r.text}"
    print(f"  -> Accepted (202). Processing...")


def poll_responses(client_name: str, timeout: int = 30) -> list[bytes]:
    """Poll for responses, return list of PCM audio data."""
    responses = []
    start = time.time()
    consecutive_empty = 0

    while time.time() - start < timeout:
        r = requests.get(f"{SERVER}/poll", headers=headers(client_name))

        if r.status_code == 200:
            responses.append(r.content)
            duration = len(r.content) / (16000 * 2)
            print(f"  <- [{client_name}] Got response: {len(r.content)} bytes ({duration:.1f}s)")
            consecutive_empty = 0
            continue
        elif r.status_code == 204:
            consecutive_empty += 1
            if consecutive_empty > 3 and responses:
                break  # got responses, no more coming
            time.sleep(1)
        else:
            print(f"  <- [{client_name}] Error: {r.status_code} {r.text}")
            time.sleep(1)

    return responses


def save_wav(pcm_data: bytes, path: str):
    """Save raw 16-bit 16kHz mono PCM as WAV."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm_data)


def play_pcm(pcm_data: bytes, label: str = "", save_path: str = ""):
    """Play raw 16-bit 16kHz mono PCM using afplay (macOS)."""
    if save_path:
        save_wav(pcm_data, save_path)
        print(f"  Saved: {save_path}")

    wav_path = save_path or "/tmp/talker_test.wav"
    if not save_path:
        save_wav(pcm_data, wav_path)

    if label:
        print(f"  Playing: {label}")
    subprocess.run(["afplay", wav_path], check=True)


def drain_all_queues():
    """Clear any pending messages from all clients."""
    print("Draining queues...")
    for client_name in CLIENTS:
        while True:
            r = requests.get(f"{SERVER}/poll", headers=headers(client_name))
            if r.status_code != 200:
                break
    print("  All queues empty.\n")


def run_scenario(scenario: dict, scenario_idx: int):
    # Slug for output directory: e.g. "01_kitchen_announces_dinner"
    slug = f"{scenario_idx:02d}_{scenario['name'].lower().replace(' ', '_')}"
    out_dir = os.path.join("results", slug)

    print(f"\n{'='*60}")
    print(f"SCENARIO: {scenario['name']}")
    print(f"  {scenario['description']}")
    print(f"  Output: {out_dir}/")
    print(f"{'='*60}")

    audio_path = scenario["audio"]
    if not os.path.exists(audio_path):
        print(f"  ERROR: Audio file not found: {audio_path}")
        print(f"  Run 'python generate_audio.py' first.")
        return False

    # Send the voice message
    send_voice(scenario["sender"], audio_path)

    # Wait a moment for processing
    print("  Waiting for AI processing...")
    time.sleep(3)

    # Poll for responses on expected clients
    all_ok = True
    for client_name in scenario["expect_responses_on"]:
        print(f"\n  Polling {client_name}...")
        responses = poll_responses(client_name, timeout=30)

        if not responses:
            print(f"  WARNING: No response received on {client_name}")
            all_ok = False
        else:
            for i, pcm in enumerate(responses):
                wav_path = os.path.join(out_dir, f"{client_name}_{i+1}.wav")
                play_pcm(pcm, label=f"{client_name} response {i+1}", save_path=wav_path)

    # Also check if unexpected clients got messages
    for client_name in CLIENTS:
        if client_name in scenario["expect_responses_on"]:
            continue
        if client_name == scenario["sender"]:
            responses = poll_responses(client_name, timeout=3)
            if responses:
                print(f"\n  (Sender {client_name} also got {len(responses)} response(s))")
                for i, pcm in enumerate(responses):
                    wav_path = os.path.join(out_dir, f"{client_name}_{i+1}.wav")
                    play_pcm(pcm, label=f"{client_name} response {i+1}", save_path=wav_path)

    return all_ok


def main():
    # Check audio files exist
    missing = [s["audio"] for s in SCENARIOS if not os.path.exists(s["audio"])]
    if missing:
        print("Missing audio files. Generating...")
        subprocess.run([sys.executable, "generate_audio.py"], check=True)
        print()

    drain_all_queues()

    results = []
    for i, scenario in enumerate(SCENARIOS):
        ok = run_scenario(scenario, i + 1)
        results.append((scenario["name"], ok))
        print()
        # Small delay between scenarios
        time.sleep(2)

    # Summary
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
