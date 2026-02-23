"""
Riot Tournament Code Stats → Google Sheets
===========================================
Pulls match stats from Riot API using tournament codes
and writes them to a Google Sheets spreadsheet.

SETUP:
1. pip install requests gspread google-auth python-dotenv
2. Copy .env.example to .env and fill in your values:
   - RIOT_API_KEY from https://developer.riotgames.com
   - GOOGLE_CREDENTIALS_FILE path to your service account JSON
   - SPREADSHEET_NAME name of your Google Sheet
3. Set up Google Sheets API credentials:
   - Go to https://console.cloud.google.com
   - Create a project & enable Google Sheets API
   - Create a Service Account & download the JSON key file
   - Share your Google Sheet with the service account email
4. Add your tournament codes to the TOURNAMENT_CODES list below
5. Run: python riot_tournament_stats.py
"""

import requests
import gspread
import time
import sys
import os
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ============================================================
# LOAD ENV VARIABLES
# ============================================================

load_dotenv()

RIOT_API_KEY = os.getenv("RIOT_API_KEY")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Tournament Stats")

# ============================================================
# CONFIG
# ============================================================

# Riot API region for match lookups
# Options: "americas", "europe", "asia", "sea"
REGION = "americas"

# Worksheet tab name
WORKSHEET_NAME = "Sheet1"

# One Riot ID per TEAM is enough - all 10 players' stats come back from each match
# Format: "Name#Tag"
PLAYER_RIOT_IDS = [
    # "player1#NA1",
    # "player2#NA1",
    # "player3#NA1",
    # "player4#NA1",
]

# Which day of the week are your inhouse games? (0=Monday, 6=Sunday)
GAME_DAY = 0  # Monday

# Set specific dates to pull games from, or leave empty to auto-detect last Monday
# Format: ["YYYY-MM-DD", "YYYY-MM-DD", ...]
TARGET_DATES = [
    # "2026-01-19",
    # "2026-01-26",
    # "2026-02-02",
    # "2026-02-09",
    # "2026-02-16",
]

# Timezone offset from UTC for game time (EST = -5, CST = -6, PST = -8)
GAME_TIMEZONE_OFFSET = -5  # EST

# Queue IDs to look for
# 3130 = Battlefy/tournament custom games
# 0 = regular custom games
CUSTOM_QUEUE_IDS = [0, 3130]

# Rate limiting - delay between API calls (seconds)
# Dev key: 1.2s is safe | Production key: 0.1s is usually fine
API_DELAY = 1.2

# ============================================================
# RIOT API FUNCTIONS
# ============================================================

HEADERS = {"X-Riot-Token": RIOT_API_KEY}


def get_game_time_windows(day_of_week):
    """Get start and end timestamps for all target dates."""
    tz = timezone(timedelta(hours=GAME_TIMEZONE_OFFSET))
    windows = []

    if TARGET_DATES:
        for date_str in TARGET_DATES:
            target_day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
            start = target_day.replace(hour=12, minute=0, second=0, microsecond=0)
            end = (target_day + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
            windows.append((int(start.timestamp()), int(end.timestamp())))
    else:
        today = datetime.now(tz)
        days_since = (today.weekday() - day_of_week) % 7
        if days_since == 0 and today.hour < 12:
            days_since = 7
        target_day = today - timedelta(days=days_since)
        start = target_day.replace(hour=12, minute=0, second=0, microsecond=0)
        end = (target_day + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
        windows.append((int(start.timestamp()), int(end.timestamp())))

    # Sort windows by start time (earliest first)
    windows.sort(key=lambda w: w[0])
    return windows


def get_puuid_from_riot_id(riot_id):
    """Convert a Riot ID (Name#Tag) to a PUUID."""
    parts = riot_id.split("#")
    if len(parts) != 2:
        print(f"  [ERROR] Invalid Riot ID format: '{riot_id}' — should be Name#Tag")
        return None

    game_name, tag_line = parts
    url = (
        f"https://{REGION}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    )
    resp = requests.get(url, headers=HEADERS)

    if resp.status_code == 200:
        return resp.json().get("puuid")
    else:
        print(f"  [ERROR] Failed to look up '{riot_id}': {resp.status_code} - {resp.text}")
        return None


def get_all_match_ids(puuid, earliest_timestamp):
    """
    Page through a player's match history until we pass the earliest target date.
    Returns all match IDs from now back to that date.
    """
    all_ids = []
    start_index = 0
    batch_size = 100

    while True:
        url = (
            f"https://{REGION}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        )
        params = {
            "start": start_index,
            "count": batch_size,
        }
        resp = requests.get(url, headers=HEADERS, params=params)
        time.sleep(API_DELAY)

        if resp.status_code != 200:
            print(f"    [ERROR] Failed to get matches (start={start_index}): {resp.status_code} - {resp.text}")
            break

        batch = resp.json()
        if not batch:
            print(f"    Reached end of match history")
            break

        all_ids.extend(batch)
        print(f"    Fetched {len(batch)} matches (total: {len(all_ids)}, index {start_index}-{start_index + len(batch) - 1})")

        # Check the last match timestamp to see if we've gone back far enough
        last_match_url = (
            f"https://{REGION}.api.riotgames.com"
            f"/lol/match/v5/matches/{batch[-1]}"
        )
        last_resp = requests.get(last_match_url, headers=HEADERS)
        time.sleep(API_DELAY)

        if last_resp.status_code == 200:
            last_match_data = last_resp.json()
            last_timestamp = last_match_data.get("info", {}).get("gameStartTimestamp", 0) / 1000
            tz = timezone(timedelta(hours=GAME_TIMEZONE_OFFSET))
            last_date = datetime.fromtimestamp(last_timestamp, tz=tz).strftime("%m/%d/%Y")
            earliest_date = datetime.fromtimestamp(earliest_timestamp, tz=tz).strftime("%m/%d/%Y")
            print(f"    Oldest match in batch: {last_date} (need to reach: {earliest_date})")

            if last_timestamp < earliest_timestamp:
                print(f"    ✓ Reached target date range")
                break

        if len(batch) < batch_size:
            print(f"    Reached end of match history")
            break

        start_index += batch_size

    return all_ids


def get_match_details(match_id):
    """Get full match details by match ID."""
    url = (
        f"https://{REGION}.api.riotgames.com"
        f"/lol/match/v5/matches/{match_id}"
    )
    resp = requests.get(url, headers=HEADERS)

    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"  [ERROR] Failed to get match details for '{match_id}': {resp.status_code} - {resp.text}")
        return None


def is_inhouse_game(match_data, windows):
    """Check if a match is a custom/tournament game AND within any of our date windows."""
    info = match_data.get("info", {})
    queue_id = info.get("queueId", -1)
    game_type = info.get("gameType", "")

    is_custom = queue_id in CUSTOM_QUEUE_IDS or game_type == "CUSTOM_GAME"
    if not is_custom:
        return False

    game_start = info.get("gameStartTimestamp", 0) / 1000
    for start_time, end_time in windows:
        if start_time <= game_start <= end_time:
            return True

    return False


# ============================================================
# TEAM OBJECTIVE EXTRACTION
# ============================================================


def get_team_objectives(match_data):
    """
    Extract team-level objective stats from match data.
    Returns a dict keyed by teamId (100=Blue, 200=Red).
    """
    teams = match_data.get("info", {}).get("teams", [])
    team_obj = {}

    for team in teams:
        team_id = team.get("teamId")
        objectives = team.get("objectives", {})

        team_obj[team_id] = {
            "dragons": objectives.get("dragon", {}).get("kills", 0),
            "firstDragon": objectives.get("dragon", {}).get("first", False),
            "barons": objectives.get("baron", {}).get("kills", 0),
            "firstBaron": objectives.get("baron", {}).get("first", False),
            "heralds": objectives.get("riftHerald", {}).get("kills", 0),
            "firstHerald": objectives.get("riftHerald", {}).get("first", False),
            "grubs": objectives.get("horde", {}).get("kills", 0),
            "firstGrubs": objectives.get("horde", {}).get("first", False),
            "towers": objectives.get("tower", {}).get("kills", 0),
            "firstTower": objectives.get("tower", {}).get("first", False),
            "inhibitors": objectives.get("inhibitor", {}).get("kills", 0),
            "firstInhibitor": objectives.get("inhibitor", {}).get("first", False),
            "atakhan": objectives.get("atakhan", {}).get("kills", 0),
            "firstBlood": objectives.get("champion", {}).get("first", False),
        }

    return team_obj


# ============================================================
# STAT EXTRACTION
# ============================================================

STAT_HEADERS = [
    "Date", "Match ID", "Game Duration (min)",
    "Team", "Summoner Name", "Tag", "Champion", "Role",
    "Kills", "Deaths", "Assists", "KDA",
    "Double Kills", "Triple Kills", "Quadra Kills", "Penta Kills",
    "Total Damage to Champions", "Damage/min", "Physical Damage", "Magic Damage",
    "True Damage", "Damage Taken", "Damage Mitigated",
    "CS", "CS/min", "Gold Earned", "Gold/min",
    "Vision Score", "Wards Placed", "Wards Killed", "Control Wards Bought",
    "Turret Kills", "Turret Damage", "Objective Damage",
    "Team Dragons", "Team First Dragon", "Team Barons", "Team First Baron",
    "Team Heralds", "Team First Herald", "Team Grubs", "Team First Grubs",
    "Team Towers", "Team First Tower", "Team First Blood",
    "Win",
]


def extract_stats(match_data):
    """Extract player stats from match data into spreadsheet rows."""
    rows = []
    info = match_data.get("info", {})
    match_id = match_data.get("metadata", {}).get("matchId", "Unknown")
    game_duration_min = round(info.get("gameDuration", 0) / 60, 1)

    tz = timezone(timedelta(hours=GAME_TIMEZONE_OFFSET))
    game_start = info.get("gameStartTimestamp", 0) / 1000
    game_date = datetime.fromtimestamp(game_start, tz=tz).strftime("%Y-%m-%d %I:%M %p")

    team_obj = get_team_objectives(match_data)

    for p in info.get("participants", []):
        kills = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        assists = p.get("assists", 0)
        cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
        damage = p.get("totalDamageDealtToChampions", 0)
        gold = p.get("goldEarned", 0)

        kda = round((kills + assists) / max(deaths, 1), 2)
        cs_per_min = round(cs / max(game_duration_min, 1), 1)
        damage_per_min = round(damage / max(game_duration_min, 1), 0)
        gold_per_min = round(gold / max(game_duration_min, 1), 0)

        team_id = p.get("teamId", 100)
        team = "Blue" if team_id == 100 else "Red"
        t_obj = team_obj.get(team_id, {})

        row = [
            game_date, match_id, game_duration_min,
            team,
            p.get("riotIdGameName", p.get("summonerName", "Unknown")),
            p.get("riotIdTagline", ""),
            p.get("championName", "Unknown"),
            p.get("teamPosition", "Unknown"),
            kills, deaths, assists, kda,
            p.get("doubleKills", 0), p.get("tripleKills", 0),
            p.get("quadraKills", 0), p.get("pentaKills", 0),
            damage, int(damage_per_min),
            p.get("physicalDamageDealtToChampions", 0),
            p.get("magicDamageDealtToChampions", 0),
            p.get("trueDamageDealtToChampions", 0),
            p.get("totalDamageTaken", 0),
            p.get("damageSelfMitigated", 0),
            cs, cs_per_min, gold, int(gold_per_min),
            p.get("visionScore", 0), p.get("wardsPlaced", 0),
            p.get("wardsKilled", 0), p.get("visionWardsBoughtInGame", 0),
            p.get("turretKills", 0), p.get("damageDealtToTurrets", 0),
            p.get("damageDealtToObjectives", 0),
            t_obj.get("dragons", 0),
            "Yes" if t_obj.get("firstDragon") else "No",
            t_obj.get("barons", 0),
            "Yes" if t_obj.get("firstBaron") else "No",
            t_obj.get("heralds", 0),
            "Yes" if t_obj.get("firstHerald") else "No",
            t_obj.get("grubs", 0),
            "Yes" if t_obj.get("firstGrubs") else "No",
            t_obj.get("towers", 0),
            "Yes" if t_obj.get("firstTower") else "No",
            "Yes" if t_obj.get("firstBlood") else "No",
            "Win" if p.get("win") else "Loss",
        ]
        rows.append(row)

    return rows


# ============================================================
# GOOGLE SHEETS
# ============================================================


def connect_to_sheet():
    """Connect to Google Sheets and return the worksheet."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)

    try:
        spreadsheet = gc.open(SPREADSHEET_NAME)
    except gspread.SpreadsheetNotFound:
        print(f"[ERROR] Spreadsheet '{SPREADSHEET_NAME}' not found.")
        print("Make sure you've shared the sheet with your service account email.")
        sys.exit(1)

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=60)

    return worksheet


def write_to_sheet(worksheet, all_rows):
    """Write headers + data rows to the worksheet."""
    try:
        first_row = worksheet.row_values(1)
    except Exception:
        first_row = []

    if not first_row or first_row[0] != STAT_HEADERS[0]:
        worksheet.update('A1', [STAT_HEADERS])
        print(f"  Added headers to '{WORKSHEET_NAME}'")

    if all_rows:
        existing = worksheet.get_all_values()
        next_row = len(existing) + 1
        worksheet.update(f'A{next_row}', all_rows)
        print(f"  Wrote {len(all_rows)} rows to '{WORKSHEET_NAME}'")
    else:
        print("  No data to write.")


# ============================================================
# MAIN
# ============================================================


def main():
    if not RIOT_API_KEY or RIOT_API_KEY == "your-riot-api-key-here":
        print("[ERROR] Riot API key not set.")
        print("Add your key to the .env file: RIOT_API_KEY=your-key-here")
        return

    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        print(f"[ERROR] Google credentials file not found: {GOOGLE_CREDENTIALS_FILE}")
        print("Download your service account JSON from Google Cloud Console.")
        return

    if not PLAYER_RIOT_IDS:
        print("[ERROR] No player Riot IDs provided!")
        return

    # Get all time windows
    windows = get_game_time_windows(GAME_DAY)
    tz = timezone(timedelta(hours=GAME_TIMEZONE_OFFSET))
    print(f"Looking for inhouse games in {len(windows)} date window(s):")
    for start_time, end_time in windows:
        start_str = datetime.fromtimestamp(start_time, tz=tz).strftime("%A %Y-%m-%d %I:%M %p EST")
        end_str = datetime.fromtimestamp(end_time, tz=tz).strftime("%A %Y-%m-%d %I:%M %p EST")
        print(f"  {start_str} → {end_str}")
    print()

    # Earliest timestamp — pagination stops once we pass this
    earliest_timestamp = windows[0][0]

    all_match_ids = set()
    all_rows = []

    # Step 1: Convert Riot IDs to PUUIDs and collect match IDs
    for i, riot_id in enumerate(PLAYER_RIOT_IDS, 1):
        print(f"[{i}/{len(PLAYER_RIOT_IDS)}] Looking up: {riot_id}")

        puuid = get_puuid_from_riot_id(riot_id)
        time.sleep(API_DELAY)

        if not puuid:
            continue

        print(f"  PUUID: {puuid[:30]}...")
        print(f"  Paginating match history back to {datetime.fromtimestamp(earliest_timestamp, tz=tz).strftime('%m/%d/%Y')}...")

        match_ids = get_all_match_ids(puuid, earliest_timestamp)

        if match_ids:
            new_ids = set(match_ids) - all_match_ids
            all_match_ids.update(match_ids)
            print(f"  Total: {len(match_ids)} matches, {len(new_ids)} new unique")
        else:
            print("  No matches found")

        print()

    print(f"{'='*60}")
    print(f"Total unique matches to check: {len(all_match_ids)}")
    print(f"{'='*60}\n")

    # Step 2: Get details for each match, filter for inhouse games on target dates
    custom_count = 0
    skipped_wrong_date = 0
    skipped_wrong_type = 0

    for i, match_id in enumerate(sorted(all_match_ids), 1):
        print(f"[{i}/{len(all_match_ids)}] Fetching match: {match_id}")
        match_data = get_match_details(match_id)
        time.sleep(API_DELAY)

        if not match_data:
            continue

        if is_inhouse_game(match_data, windows):
            custom_count += 1
            stats = extract_stats(match_data)
            all_rows.extend(stats)
            game_info = match_data.get("info", {})
            duration = round(game_info.get("gameDuration", 0) / 60, 1)
            queue = game_info.get("queueId", "?")
            game_start_ts = game_info.get("gameStartTimestamp", 0) / 1000
            game_date = datetime.fromtimestamp(game_start_ts, tz=tz).strftime("%m/%d %I:%M %p")
            print(f"  ✓ Inhouse game! {game_date} | Queue: {queue} | Duration: {duration} min | {len(stats)} players")
        else:
            info = match_data.get("info", {})
            queue_id = info.get("queueId", "?")
            game_type = info.get("gameType", "?")
            game_start_ts = info.get("gameStartTimestamp", 0) / 1000
            game_date = datetime.fromtimestamp(game_start_ts, tz=tz).strftime("%m/%d %I:%M %p")

            is_custom = queue_id in CUSTOM_QUEUE_IDS or game_type == "CUSTOM_GAME"
            if is_custom:
                skipped_wrong_date += 1
                print(f"  ✗ Custom game but wrong date ({game_date})")
            else:
                skipped_wrong_type += 1
                print(f"  ✗ Not a custom game (queueId: {queue_id})")

    print(f"\n{'='*60}")
    print(f"RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"Inhouse games found on target dates: {custom_count}")
    print(f"Custom games on other dates (skipped): {skipped_wrong_date}")
    print(f"Non-custom games (skipped): {skipped_wrong_type}")
    print(f"Total player rows to write: {len(all_rows)}")
    print(f"{'='*60}")

    # Step 3: Write to Google Sheets
    if all_rows:
        print(f"\nConnecting to Google Sheets...")
        worksheet = connect_to_sheet()
        write_to_sheet(worksheet, all_rows)
        print("\nDone! Check your spreadsheet.")
    else:
        print("\nNo inhouse games found for these date windows.")


if __name__ == "__main__":
    main()
