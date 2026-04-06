"""
Ember / Titan Esports — Riot Inhouse Stats → Google Sheets
============================================================
Multi-league version. Pulls match stats from Riot API,
writes to Google Sheets with a "League" column so the
Ember_Stats.html dashboard can filter by league.

Leagues: Scorch, Magma, Cinder, Blaze

SETUP:
1. pip install requests gspread google-auth
2. Set up Google Sheets API credentials (credentials.json)
3. Set RIOT_API_KEY, SPREADSHEET_NAME, LEAGUE_NAME
4. Add player Riot IDs to PLAYER_RIOT_IDS
5. Add game night dates to TARGET_DATES
6. Run: python ember_stats.py

Run once per league per game night, changing LEAGUE_NAME each time.
"""

import requests
import gspread
import time
import sys
import os
from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials

# ============================================================
# LOAD ENV VARIABLES
# ============================================================

RIOT_API_KEY = "PASTE_YOUR_RIOT_API_KEY_HERE"
GOOGLE_CREDENTIALS_FILE = "credentials.json"
SPREADSHEET_NAME = "Ember Tournament Stats"

# ============================================================
# CONFIG
# ============================================================

REGION = "americas"
WORKSHEET_NAME = "Sheet1"

# Which league is this run for? (Scorch, Magma, Cinder, or Blaze)
# This gets written into the "League" column for every row
LEAGUE_NAME = "Scorch"

PLAYER_RIOT_IDS = [
    # Add player Riot IDs here, e.g.:
    # "PlayerName#TAG",
]

GAME_DAY = 0  # Monday

TARGET_DATES = [
    # Add game night dates here, e.g.:
    # "2026-04-07",
]

GAME_TIMEZONE_OFFSET = -5  # EST
CUSTOM_QUEUE_IDS = [3130]
API_DELAY = 1.5

TIMELINE_INTERVALS = [5, 10, 15, 20]

# ============================================================
# CHAMPION ID → NAME MAPPING (for resolving ban championIds)
# ============================================================

# Global champion ID map (loaded once at startup)
CHAMPION_ID_MAP = {}

def fetch_champion_id_map():
    """Fetch champion ID to name mapping from Riot Data Dragon."""
    try:
        versions_url = "https://ddragon.leagueoflegends.com/api/versions.json"
        resp = requests.get(versions_url, timeout=10)
        latest_version = resp.json()[0]

        champ_url = f"https://ddragon.leagueoflegends.com/cdn/{latest_version}/data/en_US/champion.json"
        resp = requests.get(champ_url, timeout=10)
        champ_data = resp.json()["data"]

        id_to_name = {}
        for champ_name, champ_info in champ_data.items():
            champ_id = int(champ_info["key"])
            id_to_name[champ_id] = champ_name

        print(f"  Loaded {len(id_to_name)} champion ID mappings (patch {latest_version})")
        return id_to_name
    except Exception as e:
        print(f"  [WARN] Could not fetch champion ID map: {e}")
        return {}

# ============================================================
# RIOT API FUNCTIONS
# ============================================================

HEADERS = {"X-Riot-Token": RIOT_API_KEY}


def get_game_time_windows(day_of_week):
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
    windows.sort(key=lambda w: w[0])
    return windows


def get_puuid_from_riot_id(riot_id):
    parts = riot_id.split("#")
    if len(parts) != 2:
        print(f"  [ERROR] Invalid Riot ID format: '{riot_id}'")
        return None
    game_name, tag_line = parts
    url = f"https://{REGION}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code == 200:
        return resp.json().get("puuid")
    else:
        print(f"  [ERROR] Failed to look up '{riot_id}': {resp.status_code}")
        return None


def get_all_match_ids(puuid, earliest_timestamp):
    all_ids = []
    start_index = 0
    batch_size = 99
    while True:
        url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
        params = {"start": start_index, "count": batch_size}
        resp = requests.get(url, headers=HEADERS, params=params)
        time.sleep(API_DELAY)
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        all_ids.extend(batch)
        print(f"    Fetched {len(batch)} matches (total: {len(all_ids)})")
        last_match_url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{batch[-1]}"
        last_resp = requests.get(last_match_url, headers=HEADERS)
        time.sleep(API_DELAY)
        if last_resp.status_code == 200:
            last_ts = last_resp.json().get("info", {}).get("gameStartTimestamp", 0) / 1000
            if last_ts < earliest_timestamp:
                break
        if len(batch) < batch_size:
            break
        start_index += batch_size
    return all_ids


def get_match_details(match_id):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"  [ERROR] Match details failed for '{match_id}': {resp.status_code}")
        return None


def is_inhouse_game(match_data, windows):
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
# TIMELINE DATA
# ============================================================

def get_match_timeline(match_id):
    url = f"https://{REGION}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    resp = requests.get(url, headers=HEADERS)
    time.sleep(API_DELAY)
    if resp.status_code == 200:
        return resp.json()
    else:
        print(f"  [WARN] Could not fetch timeline for {match_id}: {resp.status_code}")
        return None


def parse_timeline_data(timeline_data):
    solo_kills = {}
    interval_stats = {}
    turret_plates = {}
    first_blood_info = None
    level6_timestamps = {}
    if not timeline_data:
        return solo_kills, interval_stats, turret_plates, first_blood_info, level6_timestamps
    frames = timeline_data.get("info", {}).get("frames", [])
    for frame in frames:
        timestamp_ms = frame.get("timestamp", 0)
        minute = round(timestamp_ms / 60000)
        if minute in TIMELINE_INTERVALS:
            participant_frames = frame.get("participantFrames", {})
            for pid_str, pf in participant_frames.items():
                pid = int(pid_str)
                cs = pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0)
                gold = pf.get("totalGold", 0)
                xp = pf.get("xp", 0)
                if pid not in interval_stats:
                    interval_stats[pid] = {}
                if minute not in interval_stats[pid]:
                    interval_stats[pid][minute] = {"cs": cs, "gold": gold, "xp": xp}
        for event in frame.get("events", []):
            event_type = event.get("type")
            if event_type == "CHAMPION_KILL":
                killer_id = event.get("killerId", 0)
                assisting = event.get("assistingParticipantIds", [])
                if killer_id > 0 and len(assisting) == 0:
                    solo_kills[killer_id] = solo_kills.get(killer_id, 0) + 1
            if event_type == "CHAMPION_SPECIAL_KILL":
                kill_type = event.get("killType", "")
                if kill_type == "KILL_FIRST_BLOOD" and first_blood_info is None:
                    first_blood_info = {
                        "killerId": event.get("killerId", 0),
                        "timestamp_min": round(event.get("timestamp", 0) / 60000, 1),
                    }
            if event_type == "TURRET_PLATE_DESTROYED":
                destroyer_id = event.get("killerId", 0)
                if destroyer_id > 0:
                    turret_plates[destroyer_id] = turret_plates.get(destroyer_id, 0) + 1
            if event_type == "LEVEL_UP":
                level = event.get("level", 0)
                pid = event.get("participantId", 0)
                if level == 6 and pid > 0 and pid not in level6_timestamps:
                    level6_timestamps[pid] = round(event.get("timestamp", 0) / 60000, 1)
    return solo_kills, interval_stats, turret_plates, first_blood_info, level6_timestamps


# ============================================================
# TEAM OBJECTIVES & BANS
# ============================================================

def get_team_objectives(match_data):
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
            "teamKills": objectives.get("champion", {}).get("kills", 0),
        }
    return team_obj


def get_team_bans(match_data):
    """
    Extract bans from match data.
    Riot API: match_data.info.teams[].bans[] = [{championId, pickTurn}]
    Returns: {teamId: [list of champion names]}
    """
    teams = match_data.get("info", {}).get("teams", [])
    team_bans = {}
    for team in teams:
        team_id = team.get("teamId")
        bans = team.get("bans", [])
        ban_names = []
        for ban in bans:
            champ_id = ban.get("championId", -1)
            if champ_id > 0 and champ_id in CHAMPION_ID_MAP:
                ban_names.append(CHAMPION_ID_MAP[champ_id])
            elif champ_id > 0:
                ban_names.append(f"ChampID_{champ_id}")
            # championId of -1 means no ban was made
        team_bans[team_id] = ban_names
    return team_bans


def compute_team_totals(participants):
    team_totals = {}
    for p in participants:
        team_id = p.get("teamId", 100)
        if team_id not in team_totals:
            team_totals[team_id] = {"kills": 0, "damage": 0, "gold": 0}
        team_totals[team_id]["kills"] += p.get("kills", 0)
        team_totals[team_id]["damage"] += p.get("totalDamageDealtToChampions", 0)
        team_totals[team_id]["gold"] += p.get("goldEarned", 0)
    return team_totals


# ============================================================
# STAT EXTRACTION
# ============================================================

STAT_HEADERS = [
    "Date", "Match ID", "Game Duration (min)",
    "Team", "Teams", "Summoner Name", "Tag", "Champion", "Role", "Champion Level",
    "Kills", "Deaths", "Assists", "KDA", "Solo Kills", "Kill Participation %",
    "Double Kills", "Triple Kills", "Quadra Kills", "Penta Kills",
    "Largest Multi Kill", "Largest Killing Spree",
    "First Blood Kill", "First Blood Assist",
    "Total Damage to Champions", "Damage/min", "Damage Share %",
    "Physical Damage", "Magic Damage", "True Damage",
    "Largest Critical Strike", "Damage Per Gold",
    "Damage Taken", "Damage Taken/min", "Damage Mitigated",
    "Total Healing", "Healing on Teammates", "Shielding on Teammates",
    "Time CCing Others (s)", "Total CC Dealt (s)",
    "Gold Earned", "Gold/min", "Gold Share %",
    "Gold Spent", "Consumables Purchased", "Items Purchased",
    "CS", "CS/min", "Lane Minions Killed", "Neutral Minions Killed",
    "CS@5", "CS@10", "CS@15", "CS@20",
    "Gold@5", "Gold@10", "Gold@15", "Gold@20",
    "XP@5", "XP@10", "XP@15", "XP@20",
    "Turret Plates Destroyed", "Level 6 Timing (min)",
    "Vision Score", "Vision Score/min",
    "Wards Placed", "Wards Killed", "Control Wards Bought",
    "Detector Wards Placed", "Stealth Wards Placed",
    "Turret Kills", "Turret Damage", "Objective Damage",
    "Inhibitor Kills", "Nexus Kills",
    "Objectives Stolen", "Objectives Stolen Assists",
    "Baron Kills", "Dragon Kills",
    "Spell1 Casts (Q)", "Spell2 Casts (W)", "Spell3 Casts (E)", "Spell4 Casts (R)",
    "Summoner1 Casts", "Summoner2 Casts",
    "Longest Time Alive (s)", "Total Time Dead (s)",
    "Lane Minions First 10 Min", "Jungle CS Before 10 Min",
    "Max CS Advantage on Lane Opponent", "Max Level Lead on Lane Opponent",
    "Skillshots Hit", "Skillshots Dodged",
    "Damage Per Minute (challenges)", "Team Damage %",
    "KDA (challenges)", "Kill Participation (challenges)",
    "Effective Heal and Shield", "Bounty Gold",
    "Vision Score Advantage Over Lane Opponent",
    "Control Wards Placed (challenges)", "Wards Guarded",
    "First Turret Killed", "First Turret Killed Assist",
    "Turret Plates Taken (challenges)", "Solo Turrets Late Game", "Turret Takedowns",
    "Game Ended In Surrender", "Game Ended In Early Surrender",
    "All In Pings", "Assist Me Pings", "Danger Pings",
    "Enemy Missing Pings", "Enemy Vision Pings",
    "On My Way Pings", "Push Pings", "Need Vision Pings",
    "Team Dragons", "Team First Dragon", "Team Barons", "Team First Baron",
    "Team Heralds", "Team First Herald", "Team Grubs", "Team First Grubs",
    "Team Towers", "Team First Tower", "Team First Blood",
    "Win",
    "Season", "Season Phase", "League",
    # ── Bans (same for all 5 players on that team in that match) ──
    "Ban 1", "Ban 2", "Ban 3", "Ban 4", "Ban 5",
]


def extract_stats(match_data, solo_kills=None, interval_stats=None,
                   turret_plates=None, first_blood_info=None, level6_timestamps=None):
    if solo_kills is None: solo_kills = {}
    if interval_stats is None: interval_stats = {}
    if turret_plates is None: turret_plates = {}
    if level6_timestamps is None: level6_timestamps = {}

    rows = []
    info = match_data.get("info", {})
    match_id = match_data.get("metadata", {}).get("matchId", "Unknown")
    game_duration_min = round(info.get("gameDuration", 0) / 60, 1)

    tz = timezone(timedelta(hours=GAME_TIMEZONE_OFFSET))
    game_start = info.get("gameStartTimestamp", 0) / 1000
    game_date = datetime.fromtimestamp(game_start, tz=tz).strftime("%Y-%m-%d %I:%M %p")

    team_obj = get_team_objectives(match_data)
    team_bans = get_team_bans(match_data)
    participants = info.get("participants", [])
    team_totals = compute_team_totals(participants)

    for p in participants:
        kills = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        assists = p.get("assists", 0)
        lane_cs = p.get("totalMinionsKilled", 0)
        jungle_cs = p.get("neutralMinionsKilled", 0)
        cs = lane_cs + jungle_cs
        damage = p.get("totalDamageDealtToChampions", 0)
        gold = p.get("goldEarned", 0)

        kda = round((kills + assists) / max(deaths, 1), 2)
        cs_per_min = round(cs / max(game_duration_min, 1), 1)
        damage_per_min = round(damage / max(game_duration_min, 1), 0)
        gold_per_min = round(gold / max(game_duration_min, 1), 0)

        team_id = p.get("teamId", 100)
        team = "Blue" if team_id == 100 else "Red"
        t_obj = team_obj.get(team_id, {})
        t_totals = team_totals.get(team_id, {"kills": 0, "damage": 0, "gold": 0})

        # Bans for this player's team (padded to 5)
        my_bans = team_bans.get(team_id, [])
        while len(my_bans) < 5:
            my_bans.append("")

        participant_id = p.get("participantId")
        challenges = p.get("challenges", {})

        kill_participation = round((kills + assists) / max(t_totals["kills"], 1) * 100, 1)
        damage_share = round(damage / max(t_totals["damage"], 1) * 100, 1)
        gold_share = round(gold / max(t_totals["gold"], 1) * 100, 1)
        damage_per_gold = round(damage / max(gold, 1), 2)
        damage_taken = p.get("totalDamageTaken", 0)
        damage_taken_per_min = round(damage_taken / max(game_duration_min, 1), 0)
        vision_score = p.get("visionScore", 0)
        vision_per_min = round(vision_score / max(game_duration_min, 1), 2)

        player_solo_kills = solo_kills.get(participant_id, 0)
        player_plates = turret_plates.get(participant_id, 0)
        player_lvl6 = level6_timestamps.get(participant_id, "")

        p_intervals = interval_stats.get(participant_id, {})
        cs_at, gold_at, xp_at = [], [], []
        for mins in TIMELINE_INTERVALS:
            snapshot = p_intervals.get(mins, {})
            if snapshot and game_duration_min >= mins:
                cs_at.append(snapshot.get("cs", ""))
                gold_at.append(snapshot.get("gold", ""))
                xp_at.append(snapshot.get("xp", ""))
            else:
                cs_at.append("")
                gold_at.append("")
                xp_at.append("")

        row = [
            game_date, match_id, game_duration_min,
            team, "", # Teams (fill manually per org team name)
            p.get("riotIdGameName", p.get("summonerName", "Unknown")),
            p.get("riotIdTagline", ""),
            p.get("championName", "Unknown"),
            p.get("teamPosition", "Unknown"),
            p.get("champLevel", 0),
            kills, deaths, assists, kda, player_solo_kills, kill_participation,
            p.get("doubleKills", 0), p.get("tripleKills", 0),
            p.get("quadraKills", 0), p.get("pentaKills", 0),
            p.get("largestMultiKill", 0), p.get("largestKillingSpree", 0),
            "Yes" if p.get("firstBloodKill") else "No",
            "Yes" if p.get("firstBloodAssist") else "No",
            damage, int(damage_per_min), damage_share,
            p.get("physicalDamageDealtToChampions", 0),
            p.get("magicDamageDealtToChampions", 0),
            p.get("trueDamageDealtToChampions", 0),
            p.get("largestCriticalStrike", 0), damage_per_gold,
            damage_taken, int(damage_taken_per_min), p.get("damageSelfMitigated", 0),
            p.get("totalHeal", 0), p.get("totalHealsOnTeammates", 0),
            p.get("totalDamageShieldedOnTeammates", 0),
            p.get("timeCCingOthers", 0), p.get("totalTimeCCDealt", 0),
            gold, int(gold_per_min), gold_share,
            p.get("goldSpent", 0), p.get("consumablesPurchased", 0), p.get("itemsPurchased", 0),
            cs, cs_per_min, lane_cs, jungle_cs,
            *cs_at, *gold_at, *xp_at,
            player_plates, player_lvl6,
            vision_score, vision_per_min,
            p.get("wardsPlaced", 0), p.get("wardsKilled", 0),
            p.get("visionWardsBoughtInGame", 0), p.get("detectorWardsPlaced", 0),
            p.get("sightWardsBoughtInGame", 0),
            p.get("turretKills", 0), p.get("damageDealtToTurrets", 0),
            p.get("damageDealtToObjectives", 0),
            p.get("inhibitorKills", 0), p.get("nexusKills", 0),
            p.get("objectivesStolen", 0), p.get("objectivesStolenAssists", 0),
            p.get("baronKills", 0), p.get("dragonKills", 0),
            p.get("spell1Casts", 0), p.get("spell2Casts", 0),
            p.get("spell3Casts", 0), p.get("spell4Casts", 0),
            p.get("summoner1Casts", 0), p.get("summoner2Casts", 0),
            p.get("longestTimeSpentLiving", 0), p.get("totalTimeSpentDead", 0),
            challenges.get("laneMinionsFirst10Minutes", ""),
            challenges.get("jungleCsBefore10Minutes", ""),
            challenges.get("maxCsAdvantageOnLaneOpponent", ""),
            challenges.get("maxLevelLeadLaneOpponent", ""),
            challenges.get("skillshotsHit", ""), challenges.get("skillshotsDodged", ""),
            challenges.get("damagePerMinute", ""), challenges.get("teamDamagePercentage", ""),
            challenges.get("kda", ""), challenges.get("killParticipation", ""),
            challenges.get("effectiveHealAndShielding", ""), challenges.get("bountyGold", ""),
            challenges.get("visionScoreAdvantageLaneOpponent", ""),
            challenges.get("controlWardsPlaced", ""), challenges.get("wardsGuarded", ""),
            "Yes" if challenges.get("firstTurretKilled") else ("No" if challenges.get("firstTurretKilled") is not None else ""),
            "Yes" if challenges.get("firstTurretKilledAssist") else ("No" if challenges.get("firstTurretKilledAssist") is not None else ""),
            challenges.get("turretPlatesTaken", ""),
            challenges.get("soloTurretsLategame", ""),
            challenges.get("turretTakedowns", ""),
            "Yes" if p.get("gameEndedInSurrender") else "No",
            "Yes" if p.get("gameEndedInEarlySurrender") else "No",
            p.get("allInPings", 0), p.get("assistMePings", 0),
            p.get("dangerPings", 0), p.get("enemyMissingPings", 0),
            p.get("enemyVisionPings", 0), p.get("onMyWayPings", 0),
            p.get("pushPings", 0), p.get("needVisionPings", 0),
            t_obj.get("dragons", 0), "Yes" if t_obj.get("firstDragon") else "No",
            t_obj.get("barons", 0), "Yes" if t_obj.get("firstBaron") else "No",
            t_obj.get("heralds", 0), "Yes" if t_obj.get("firstHerald") else "No",
            t_obj.get("grubs", 0), "Yes" if t_obj.get("firstGrubs") else "No",
            t_obj.get("towers", 0), "Yes" if t_obj.get("firstTower") else "No",
            "Yes" if t_obj.get("firstBlood") else "No",
            "Win" if p.get("win") else "Loss",
            "", "",  # Season, Season Phase (fill manually)
            LEAGUE_NAME,  # League
            *my_bans[:5],  # Ban 1 through Ban 5
        ]
        rows.append(row)

    return rows


# ============================================================
# GOOGLE SHEETS
# ============================================================

def connect_to_sheet():
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
        sys.exit(1)
    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=200)
    return worksheet


def write_to_sheet(worksheet, all_rows):
    try:
        first_row = worksheet.row_values(1)
    except Exception:
        first_row = []

    if not first_row or first_row[0] != STAT_HEADERS[0]:
        worksheet.update('A1', [STAT_HEADERS])
        print(f"  Added headers to '{WORKSHEET_NAME}'")

    if all_rows:
        existing = worksheet.get_all_values()
        existing_keys = set()
        match_id_col = STAT_HEADERS.index("Match ID")
        name_col = STAT_HEADERS.index("Summoner Name")
        for row in existing[1:]:
            if len(row) > max(match_id_col, name_col):
                existing_keys.add(f"{row[match_id_col]}_{row[name_col]}")

        new_rows = []
        dupes = 0
        for row in all_rows:
            key = f"{row[match_id_col]}_{row[name_col]}"
            if key in existing_keys:
                dupes += 1
            else:
                new_rows.append(row)

        if dupes:
            print(f"  Skipped {dupes} duplicate rows already in sheet")
        if new_rows:
            next_row = len(existing) + 1
            worksheet.update(f'A{next_row}', new_rows)
            print(f"  Wrote {len(new_rows)} new rows to '{WORKSHEET_NAME}'")
        else:
            print("  No new data to write.")
    else:
        print("  No data to write.")


# ============================================================
# MATCH IDS
# ============================================================

MATCH_IDS = [
    # Add match IDs here for direct lookup mode, e.g.:
    # "NA1_1234567890",
]


# ============================================================
# HELPER
# ============================================================

def fetch_and_extract(match_id, match_data):
    print(f"  Fetching timeline data...")
    timeline_data = get_match_timeline(match_id)
    solo_kills, interval_stats, turret_plates, first_blood_info, level6_timestamps = parse_timeline_data(timeline_data)

    total_solos = sum(solo_kills.values())
    total_plates = sum(turret_plates.values())
    print(f"  Timeline: {total_solos} solo kill(s), {total_plates} plate(s) destroyed")

    # Log bans
    team_bans = get_team_bans(match_data)
    for tid, bans in team_bans.items():
        side = "Blue" if tid == 100 else "Red"
        ban_list = [b for b in bans if b]
        if ban_list:
            print(f"  {side} bans: {', '.join(ban_list)}")

    stats = extract_stats(
        match_data, solo_kills=solo_kills, interval_stats=interval_stats,
        turret_plates=turret_plates, first_blood_info=first_blood_info,
        level6_timestamps=level6_timestamps,
    )
    return stats


# ============================================================
# MAIN
# ============================================================

def main():
    global CHAMPION_ID_MAP

    if not RIOT_API_KEY or RIOT_API_KEY == "your-riot-api-key-here":
        print("[ERROR] Riot API key not set.")
        return
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        print(f"[ERROR] Google credentials file not found: {GOOGLE_CREDENTIALS_FILE}")
        return

    # Load champion ID map for resolving ban championIds to names
    print("Loading champion ID mappings from Data Dragon...")
    CHAMPION_ID_MAP = fetch_champion_id_map()
    print()

    print(f"Total columns per row: {len(STAT_HEADERS)}")
    print()

    if MATCH_IDS:
        print(f"MATCH ID LOOKUP MODE — Fetching {len(MATCH_IDS)} match(es) directly")
        print(f"{'='*60}\n")

        all_rows = []
        for i, match_id in enumerate(MATCH_IDS, 1):
            print(f"[{i}/{len(MATCH_IDS)}] Fetching: {match_id}")
            match_data = get_match_details(match_id)
            time.sleep(API_DELAY)
            if not match_data:
                continue

            info = match_data.get("info", {})
            duration = round(info.get("gameDuration", 0) / 60, 1)
            tz = timezone(timedelta(hours=GAME_TIMEZONE_OFFSET))
            game_start_ts = info.get("gameStartTimestamp", 0) / 1000
            game_date = datetime.fromtimestamp(game_start_ts, tz=tz).strftime("%Y-%m-%d %I:%M %p")
            players = len(info.get("participants", []))

            print(f"  Date: {game_date} | Duration: {duration}m | Players: {players}")

            stats = fetch_and_extract(match_id, match_data)
            all_rows.extend(stats)
            print(f"  ✓ Extracted {len(stats)} player rows\n")

        print(f"{'='*60}")
        print(f"Total player rows: {len(all_rows)}")

        if all_rows:
            print(f"\nConnecting to Google Sheets...")
            worksheet = connect_to_sheet()
            write_to_sheet(worksheet, all_rows)
            print("\nDone! Check your spreadsheet.")
        return

    # Normal date-based mode (unchanged)
    if not PLAYER_RIOT_IDS:
        print("[ERROR] No player Riot IDs provided!")
        return

    windows = get_game_time_windows(GAME_DAY)
    tz = timezone(timedelta(hours=GAME_TIMEZONE_OFFSET))
    earliest_timestamp = windows[0][0]
    all_match_ids = set()
    all_rows = []

    for i, riot_id in enumerate(PLAYER_RIOT_IDS, 1):
        print(f"[{i}/{len(PLAYER_RIOT_IDS)}] Looking up: {riot_id}")
        puuid = get_puuid_from_riot_id(riot_id)
        time.sleep(API_DELAY)
        if not puuid:
            continue
        match_ids = get_all_match_ids(puuid, earliest_timestamp)
        if match_ids:
            new_ids = set(match_ids) - all_match_ids
            all_match_ids.update(match_ids)
            print(f"  {len(new_ids)} new unique matches")
        print()

    custom_count = 0
    for i, match_id in enumerate(sorted(all_match_ids), 1):
        print(f"[{i}/{len(all_match_ids)}] Fetching: {match_id}")
        match_data = get_match_details(match_id)
        time.sleep(API_DELAY)
        if not match_data:
            continue
        if is_inhouse_game(match_data, windows):
            custom_count += 1
            stats = fetch_and_extract(match_id, match_data)
            all_rows.extend(stats)
            print(f"  ✓ Inhouse game! {len(stats)} players")

    print(f"\nInhouse games found: {custom_count}")
    print(f"Total rows: {len(all_rows)}")

    if all_rows:
        worksheet = connect_to_sheet()
        write_to_sheet(worksheet, all_rows)
        print("\nDone!")


if __name__ == "__main__":
    main()