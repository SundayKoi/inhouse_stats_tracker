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

# Your tournament codes - add as many as you need
TOURNAMENT_CODES = [
    # "NA1234-tournament-code-1",
    # "NA1234-tournament-code-2",
    # "NA1234-tournament-code-3",
]

# Rate limiting - delay between API calls (seconds)
# Dev key: 1.2s is safe | Production key: 0.1s is usually fine
API_DELAY = 1.2

# ============================================================
# RIOT API FUNCTIONS
# ============================================================

HEADERS = {"X-Riot-Token": RIOT_API_KEY}


def get_match_ids_by_tournament_code(tournament_code):
    """Get match IDs associated with a tournament code."""
    url = (
        f"https://{REGION}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/tournament-codes/{tournament_code}"
    )
    resp = requests.get(url, headers=HEADERS)

    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"  [ERROR] Failed to get matches for code '{tournament_code}': {resp.status_code} - {resp.text}")
        return []


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


# ============================================================
# STAT EXTRACTION
# ============================================================

STAT_HEADERS = [
    "Tournament Code",
    "Match ID",
    "Game Duration (min)",
    "Team",
    "Summoner Name",
    "Tag",
    "Champion",
    "Role",
    "Kills",
    "Deaths",
    "Assists",
    "KDA",
    "CS",
    "CS/min",
    "Total Damage to Champions",
    "Damage/min",
    "Gold Earned",
    "Vision Score",
    "Wards Placed",
    "Wards Killed",
    "Win",
]


def extract_stats(match_data, tournament_code):
    """Extract player stats from match data into spreadsheet rows."""
    rows = []
    info = match_data.get("info", {})
    match_id = match_data.get("metadata", {}).get("matchId", "Unknown")
    game_duration_min = round(info.get("gameDuration", 0) / 60, 1)

    for p in info.get("participants", []):
        kills = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        assists = p.get("assists", 0)
        cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
        damage = p.get("totalDamageDealtToChampions", 0)

        # Calculate KDA (handle divide by zero)
        kda = round((kills + assists) / max(deaths, 1), 2)
        cs_per_min = round(cs / max(game_duration_min, 1), 1)
        damage_per_min = round(damage / max(game_duration_min, 1), 0)

        # Team side
        team = "Blue" if p.get("teamId") == 100 else "Red"

        row = [
            tournament_code,
            match_id,
            game_duration_min,
            team,
            p.get("riotIdGameName", p.get("summonerName", "Unknown")),
            p.get("riotIdTagline", ""),
            p.get("championName", "Unknown"),
            p.get("teamPosition", "Unknown"),
            kills,
            deaths,
            assists,
            kda,
            cs,
            cs_per_min,
            damage,
            int(damage_per_min),
            p.get("goldEarned", 0),
            p.get("visionScore", 0),
            p.get("wardsPlaced", 0),
            p.get("wardsKilled", 0),
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
        worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=25)

    return worksheet


def write_to_sheet(worksheet, all_rows):
    """Write headers + data rows to the worksheet."""
    existing = worksheet.get_all_values()

    if not existing:
        worksheet.append_row(STAT_HEADERS)
        print(f"  Added headers to '{WORKSHEET_NAME}'")

    if all_rows:
        worksheet.append_rows(all_rows)
        print(f"  Wrote {len(all_rows)} rows to '{WORKSHEET_NAME}'")
    else:
        print("  No data to write.")


# ============================================================
# MAIN
# ============================================================


def main():
    # Validate environment
    if not RIOT_API_KEY or RIOT_API_KEY == "your-riot-api-key-here":
        print("[ERROR] Riot API key not set.")
        print("Add your key to the .env file: RIOT_API_KEY=your-key-here")
        return

    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        print(f"[ERROR] Google credentials file not found: {GOOGLE_CREDENTIALS_FILE}")
        print("Download your service account JSON from Google Cloud Console.")
        return

    if not TOURNAMENT_CODES:
        print("[ERROR] No tournament codes provided!")
        print("Add your codes to the TOURNAMENT_CODES list in the script.")
        return

    print(f"Processing {len(TOURNAMENT_CODES)} tournament code(s)...\n")

    all_rows = []

    for i, code in enumerate(TOURNAMENT_CODES, 1):
        print(f"[{i}/{len(TOURNAMENT_CODES)}] Tournament Code: {code}")

        # Step 1: Get match IDs for this tournament code
        match_ids = get_match_ids_by_tournament_code(code)
        time.sleep(API_DELAY)

        if not match_ids:
            print("  No matches found for this code.\n")
            continue

        print(f"  Found {len(match_ids)} match(es)")

        # Step 2: Get details for each match
        for match_id in match_ids:
            print(f"  Fetching match: {match_id}")
            match_data = get_match_details(match_id)
            time.sleep(API_DELAY)

            if match_data:
                stats = extract_stats(match_data, code)
                all_rows.extend(stats)
                print(f"    Extracted stats for {len(stats)} players")

        print()

    # Step 3: Write to Google Sheets
    if all_rows:
        print(f"Total rows to write: {len(all_rows)}")
        print("Connecting to Google Sheets...")
        worksheet = connect_to_sheet()
        write_to_sheet(worksheet, all_rows)
        print("\nDone! Check your spreadsheet.")
    else:
        print("No data collected. Check your tournament codes and API key.")


if __name__ == "__main__":
    main()
