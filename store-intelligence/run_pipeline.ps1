$cameras = @("CAM 1", "CAM 2", "CAM 3", "CAM 4", "CAM 5")
$cam_ids = @("CAM_FLOOR_01", "CAM_FLOOR_02", "CAM_ENTRY_01", "CAM_FLOOR_03", "CAM_BILLING_03")

New-Item -ItemType Directory -Force -Path "events"

for ($i=0; $i -lt $cameras.Length; $i++) {
    $cam = $cameras[$i]
    $cam_id = $cam_ids[$i]
    $video = "../CCTV Footage/$cam.mp4"
    
    if (Test-Path $video) {
        Write-Host "Processing $cam"
        $current_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH\:mm\:ssZ")
        python pipeline/detect.py --video $video --camera-id $cam_id --store-id "STORE_BLR_002" --layout "data/store_layout.json" --output "events/${cam_id}_events.jsonl" --clip-start $current_utc --skip-frames 3 --conf 0.35 --push-to-api "http://localhost:8000/events/ingest"
    } else {
        Write-Host "Skipping $cam (not found)"
    }
}
Write-Host "Merging all events..."
python -c "
import sys, json, glob
events = []
for path in glob.glob('events/CAM_*_events.jsonl'):
    for line in open(path, encoding='utf-8'):
        if line.strip():
            try: events.append(json.loads(line))
            except: pass
events.sort(key=lambda e: e.get('timestamp', ''))
with open('events/all_events.jsonl', 'w', encoding='utf-8') as f:
    for e in events:
        f.write(json.dumps(e) + '\n')
"
Write-Host "Merged events into events/all_events.jsonl"

Write-Host "Pipeline execution finished."
