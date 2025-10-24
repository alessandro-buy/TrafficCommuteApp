import os
import json
import datetime
import pytz
import gspread
import requests
import urllib.parse
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# ============================================
# CONFIGURATION
# ============================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CONFIG_FILE = "config.json"
SHEET_NAME = "CommuteData"  # Master spreadsheet name
TIMEZONE = pytz.timezone("America/Chicago")

def now_chicago():
    return datetime.now(TIMEZONE).replace(second=0, microsecond=0)

# ============================================
# GOOGLE AUTH (Sheets + Drive)
# ============================================

def get_gspread_client():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    else:
        flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
        creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return gspread.authorize(creds)

def get_or_create_worksheet(sh, name):
    """Return worksheet if exists, else create it and ensure headers exist."""
    try:
        ws = sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=10)
        print(f"🆕 Created new sheet '{name}'")

    # --- ensure headers ---
    headers = ["Timestamp", "Day", "Route", "Duration (min)", "Length (miles)", "Directions"]
    existing_values = ws.get_all_values()

    # Treat [[""]] (Google’s default empty row) as empty
    if not existing_values or all(not any(cell.strip() for cell in row) for row in existing_values):
        ws.clear()
        ws.append_row(headers)
        print(f"🪶 Added headers to sheet '{name}'")
    elif existing_values[0] != headers and name != "LastRunLog":
        print(f"⚠️ Header mismatch in '{name}' — consider standardizing manually")

    return ws


# ============================================
# GOOGLE MAPS DIRECTIONS
# ============================================
def get_routes(origin, destination):
    api_key = os.getenv("GOOGLE_MAPS_API_KEY") or "<YOUR_API_KEY_HERE>"
    url = (
        "https://maps.googleapis.com/maps/api/directions/json"
        f"?origin={origin}"
        f"&destination={destination}"
        f"&mode=driving"
        f"&alternatives=true"
        f"&departure_time=now"
        f"&traffic_model=best_guess"
        f"&key={api_key}"
    )

    response = requests.get(url)
    if response.status_code != 200:
        print(f"⚠️ Google Maps API error: {response.text}")
        return []

    data = response.json()
    routes = data.get("routes", [])

    # Print the key data we care about for sanity
    for r in routes:
        leg = r["legs"][0]
        normal = leg["duration"]["text"]
        traffic = leg.get("duration_in_traffic", {}).get("text", "no traffic")
        print(f"🛣 {r.get('summary','N/A')}: normal={normal}, traffic={traffic}")

    return routes


# ============================================
# LOGGING ROUTES
# ============================================
def log_route_to_sheet(ws, ws_log, route_name, origin, destination, interval, days=None, start=None, end=None):
    now = now_chicago()

    # --- check if today/time are within window ---
    if days and now.strftime("%A") not in days:
        print(f"🗓 Skipping {route_name} (today not in active days)")
        return
    if start and end:
        t = now.time()
        start_t = datetime.strptime(start, "%H:%M").time()
        end_t = datetime.strptime(end, "%H:%M").time()
        if not (start_t <= t <= end_t):
            print(f"⏰ Skipping {route_name} (outside {start}-{end})")
            return

    # --- check last run from log ---
    last_dt = get_last_run_time(ws_log, route_name)
    if last_dt:
        diff = (now - last_dt).total_seconds() / 60.0
        if diff < float(interval):
            print(f"⏸ Skipping {route_name} (last logged {diff:.1f} min ago)")
            return

    # --- fetch route data (only if within window and interval) ---
    routes = get_routes(origin, destination)
    if not routes:
        print(f"⚠️ No routes found for {route_name}")
        return

    for r in routes:
        leg = r["legs"][0]

        # Extract key info
        duration_min = round(leg.get("duration_in_traffic", leg["duration"])["value"] / 60, 1)
        distance_mi = round(leg["distance"]["value"] / 1609.34, 1)
        summary = r.get("summary", "N/A")

        # --- turn-by-turn steps ---
        steps = leg.get("steps", [])
        turn_by_turn = " → ".join(
            step["html_instructions"].replace("<b>", "").replace("</b>", "")
            for step in steps
        )

        # --- clean Google Maps link (no polyline junk) ---
        encoded_origin = urllib.parse.quote(origin)
        encoded_destination = urllib.parse.quote(destination)
        maps_link = (
            f"https://www.google.com/maps/dir/?api=1"
            f"&origin={encoded_origin}"
            f"&destination={encoded_destination}"
            f"&travelmode=driving"
            f"&dir_action=navigate"
        )

        # --- append to sheet ---
        ws.append_row([
            now.strftime("%Y-%m-%d %H:%M:%S"),
            now.strftime("%A"),
            summary,
            duration_min,
            distance_mi,
            turn_by_turn,
        ])

    # --- update last run ONLY after successful log ---
    update_last_run_time(ws_log, route_name, now)


def get_last_run_time(ws_log, route_name):
    """Get last run timestamp for a given route from the LastRunLog sheet."""
    try:
        records = ws_log.get_all_records()
        for row in records:
            if row.get("Route") == route_name:
                val = row.get("LastRun")
                if val:
                    try:
                        last_dt = datetime.fromisoformat(val)
                        if last_dt.tzinfo is None:
                            last_dt = TIMEZONE.localize(last_dt)
                        return last_dt  # ✅ return inside the loop once found
                    except ValueError:
                        print(f"⚠️ Invalid date format for {route_name}: {val}")
                        return None
        # If no matching route found
        return None
    except Exception as e:
        print(f"⚠️ Could not read LastRunLog for {route_name}: {e}")
        return None


def update_last_run_time(ws_log, route_name, timestamp):
    """Update or insert the LastRun timestamp for the given route."""
    try:
        cell = ws_log.find(route_name)
        if cell:
            # Update existing row
            ws_log.update_cell(cell.row, 2, timestamp.isoformat())
        else:
            # Append a new route entry
            ws_log.append_row([route_name, timestamp.isoformat()])
            print(f"🆕 Added new route entry to LastRunLog: {route_name}")
    except Exception as e:
        print(f"⚠️ Failed to update LastRunLog for {route_name}: {e}")


# ============================================
# MAIN LOGIC
# ============================================
def run_commute_tracker():
    print("🚗 Starting commute tracker...")

    gc = get_gspread_client()
    sh = gc.open("CommuteData")  # one master spreadsheet
    ws_log = get_or_create_worksheet(sh, "LastRunLog")

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    for route in config["routes"]:
        # Each route gets its own tab, named after route["name"]
        tab_name = route["name"].replace("/", "-")  # avoid invalid characters
        ws = get_or_create_worksheet(sh, tab_name)

        log_route_to_sheet(
            ws=ws,
            ws_log=ws_log,
            route_name=route["name"],
            origin=route["origin"],
            destination=route["destination"],
            interval=route.get("interval", 15),
            start=route["start"],
            end=route["end"]
        )


# ============================================
# CLOUD FUNCTION ENTRY POINT
# ============================================

def main(request=None):
    print("🌐 Function triggered")
    try:
        if request is None:
            run_commute_tracker()
            return
        elif request.method == "GET":
            run_commute_tracker()
            return ("✅ Commute tracker executed successfully", 200)
        else:
            return ("❌ Method not allowed", 405)
    except Exception as e:
        import requests
        print("⚠️ Exception type:", type(e))
        if isinstance(e, requests.Response):
            print("🟢 Caught Response object with status:", e.status_code)
        print(f"❌ Error during execution: {e}")
        return (f"Error: {e}", 500)


# ============================================
# LOCAL RUNNER
# ============================================

if __name__ == "__main__":
    main(None)

