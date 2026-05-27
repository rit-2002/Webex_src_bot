import requests
import time
import re
from datetime import datetime, timezone

# --- CONFIGURATION ---
AUTH_TOKEN = #use your bearer token
TARGET_ROOM = # where you post the bot
SOURCE_ROOMS = #where you check messages

]

CHECK_INTERVAL = 5  # Seconds between checks

HEADERS = {'Authorization': f'Bearer {AUTH_TOKEN}', 'Content-Type': 'application/json'}
session = requests.Session()

# --- STATE ---
processed_message_ids = set()
team_members = []
sr_assignments = {}
SCRIPT_START_TIME = None

def get_person_id(email):
    try:
        url = f"https://webexapis.com/v1/people?email={email}"
        response = session.get(url, headers=HEADERS, timeout=10)
        items = response.json().get('items', [])
        return items[0]['id'] if items else None
    except: return None

def extract_sr_number(text):
    match = re.search(r'\b(7\d{8})\b', text)
    return match.group(1) if match else None

def parse_webex_time(time_str):
    try:
        return datetime.fromisoformat(time_str.replace('Z', '+00:00'))
    except:
        return None

def clean_robin_list(list_text):
    """Cleans the robin list by stopping at any non-name content."""
    raw_names = [m.strip() for m in list_text.split(',')]
    cleaned_names = []
    for name in raw_names:
        clean = re.sub(r'[🚫🔸●🔁🔔🟧🟩⬜️]', '', name).strip()
        clean = re.sub(r'\(\d+\)', '', clean).strip()
        if ' ' in clean:
            clean = clean.split()[0]
        if clean and '🚫' not in name and clean.isalnum():
            cleaned_names.append(clean)
    return cleaned_names

def load_team_from_target():
    """Loads the most recent team list AND SR assignments since script started."""
    global team_members, sr_assignments
    try:
        res = session.get(f'https://webexapis.com/v1/messages?roomId={TARGET_ROOM}&max=200', headers=HEADERS, timeout=10)
        messages = res.json().get('items', [])
        
        team_found = False
        new_sr_count = 0
        
        for msg in messages:
            text = msg.get('text', '') or ''
            markdown = msg.get('markdown', '') or ''
            msg_time = parse_webex_time(msg.get('created', ''))
            
            if not team_found:
                for source in [text, markdown]:
                    if "New robin:" in source or "Next robin:" in source:
                        marker = "New robin:" if "New robin:" in source else "Next robin:"
                        after_marker = source.split(marker, 1)[1]
                        list_text = after_marker.split('\n')[0].strip()
                        
                        cleaned_names = clean_robin_list(list_text)
                        if cleaned_names:
                            team_members = cleaned_names
                            team_found = True
                            break
            
            if msg_time and SCRIPT_START_TIME and msg_time >= SCRIPT_START_TIME:
                combined = text + " " + markdown
                sr_match = re.search(r'Added SR (\d+) for (\w+)', combined, re.IGNORECASE)
                if sr_match:
                    sr_num = sr_match.group(1)
                    engineer = sr_match.group(2)
                    if sr_num not in sr_assignments:
                        sr_assignments[sr_num] = engineer
                        new_sr_count += 1
        
        if new_sr_count > 0 or not hasattr(load_team_from_target, '_first_run_done'):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Robin: {team_members[:5]}... | SRs tracked: {len(sr_assignments)}")
            load_team_from_target._first_run_done = True
            
    except Exception as e:
        print(f"Error fetching from target: {e}")

def initialize_tracker():
    for room_id in SOURCE_ROOMS:
        try:
            res = session.get('https://webexapis.com/v1/messages', headers=HEADERS, params={'roomId': room_id, 'max': 10}, timeout=10)
            for msg in res.json().get('items', []):
                processed_message_ids.add(msg.get('id'))
        except: pass

def sync():
    global team_members
    if not team_members: 
        return

    for room_id in SOURCE_ROOMS:
        try:
            res = session.get('https://webexapis.com/v1/messages', headers=HEADERS, params={'roomId': room_id, 'max': 10}, timeout=10)
            res.raise_for_status()
            
            for msg in res.json().get('items', [])[::-1]:
                msg_id = msg.get('id')
                if msg_id not in processed_message_ids:
                    text = msg.get('text', '')
                    
                    is_forwarded = text.startswith("[Forwarded from")
                    is_sherlock_card = "see card data" in text.lower()
                    is_complete = "[ complete ]" in text.lower()
                    
                    if text and not is_forwarded and not is_sherlock_card and not is_complete:
                        sr_num = extract_sr_number(text)
                        existing_engineer = sr_assignments.get(sr_num) if sr_num else None
                        
                        if existing_engineer:
                            to_tag = [existing_engineer]
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] SR {sr_num} recalled to {existing_engineer}")
                        else:
                            to_tag = team_members[:2]
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] New SR {sr_num} tagged: {to_tag}")
                        
                        mentions = []
                        pids = []
                        for nick in to_tag:
                            pid = get_person_id(f"{nick}@cisco.com")
                            if pid:
                                mentions.append(f"<@personId:{pid}|{nick}>")
                                pids.append(pid)
                            else:
                                mentions.append(f"@{nick}")

                        recall_note = f"♻️ RECALL: Already assigned to {existing_engineer}\n\n" if existing_engineer else ""
                        payload = {
                            'roomId': TARGET_ROOM,
                            'markdown': (
                                f"{' '.join(mentions)}\n\n"
                                f"{recall_note}"
                                f"🔔 Tagged: {', '.join(to_tag)}\n\n"
                                f"{text}\n\n"
                                f"[OrigID: {msg_id}] [SR: {sr_num or 'N/A'}]"
                            ),
                            'mentionedPeople': pids
                        }
                        
                        session.post('https://webexapis.com/v1/messages', headers=HEADERS, json=payload, timeout=10)
                    
                    processed_message_ids.add(msg_id)
        except Exception as e:
            print(f"  -> Error in room {room_id}: {e}")

# --- MAIN ---
SCRIPT_START_TIME = datetime.now(timezone.utc)
print(f"Script started at: {SCRIPT_START_TIME.isoformat()}")
print(f"Checking every {CHECK_INTERVAL} seconds. SR tracking starts from now.")

load_team_from_target()
initialize_tracker()
start_time = time.time()
print(f"Sync service started. Will run for 8 hours.\n")

try:
    while time.time() - start_time < (8 * 3600):
        load_team_from_target()
        sync()
        time.sleep(CHECK_INTERVAL) 
except KeyboardInterrupt:
    print("\nStopped.")
