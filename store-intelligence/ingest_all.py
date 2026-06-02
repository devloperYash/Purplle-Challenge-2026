import json
import urllib.request
import urllib.error
import sys

try:
    with open('events/all_events.jsonl', encoding='utf-8') as f:
        events = [json.loads(l) for l in f if l.strip()]
except FileNotFoundError:
    print("Error: events/all_events.jsonl not found. Please run the pipeline first.")
    sys.exit(1)

if not events:
    print("No events to ingest in events/all_events.jsonl.")
    sys.exit(0)

batch_size = 500
print(f"Starting ingestion of {len(events)} events in batches of {batch_size}...")

for i in range(0, len(events), batch_size):
    batch = events[i:i+batch_size]
    payload = json.dumps({'events': batch}).encode('utf-8')
    req = urllib.request.Request(
        'http://localhost:8000/events/ingest',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode('utf-8'))
        print(f"Batch {i//batch_size + 1}: accepted={result['accepted']}, rejected={result['rejected']}, duplicates={result['duplicates']}")
    except urllib.error.URLError as e:
        print(f"\nError: Connection to backend failed: {e.reason}")
        print("Please check if the FastAPI backend is running (./run_backend.ps1) and listening on port 8000.")
        sys.exit(1)

print("\nDone!")
