# 🏆 Riot Tournament Stats Tracker

Automated tool that pulls post-game stats from League of Legends tournament code matches and writes them to a Google Sheets spreadsheet. Built for tracking inhouse league and scrim performance.

## Features

- Fetches match data from Riot's Tournament API using tournament codes
- Extracts detailed player stats: KDA, CS, damage, vision, gold, and more
- Writes everything to Google Sheets for easy tracking and analysis
- Rate-limited API calls to stay within Riot's limits
- Environment variable support for secure key management

## Stats Tracked

| Stat | Description |
|------|-------------|
| Summoner Name / Tag | Player's Riot ID |
| Champion | Champion played |
| Role | Position played |
| K / D / A | Kills, Deaths, Assists |
| KDA | (Kills + Assists) / Deaths |
| CS | Total minions + jungle camps |
| CS/min | CS per minute |
| Damage | Total damage to champions |
| Damage/min | Damage per minute |
| Gold | Total gold earned |
| Vision Score | Overall vision contribution |
| Wards Placed / Killed | Ward stats |
| Win/Loss | Game result |

## Setup

### Prerequisites

- Python 3.8+
- Riot Games API key with Tournament API access
- Google Cloud service account with Sheets API enabled

### Installation

```bash
git clone https://github.com/yourusername/riot-tournament-stats.git
cd riot-tournament-stats
pip install requests gspread google-auth python-dotenv
```

### Configuration

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Fill in your `.env` file:
```
RIOT_API_KEY=your-riot-api-key
GOOGLE_CREDENTIALS_FILE=credentials.json
SPREADSHEET_NAME=Tournament Stats
```

3. Place your Google service account `credentials.json` in the project root.

4. Share your Google Sheet with the service account email (found in your credentials JSON).

5. Add your tournament codes to the `TOURNAMENT_CODES` list in the script.

### Usage

```bash
python riot_tournament_stats.py
```

## How It Works

1. Loops through each tournament code
2. Retrieves the match ID(s) linked to each code via Riot's Tournament API
3. Fetches full match details from Match-v5
4. Extracts player stats and calculates derived metrics (KDA, CS/min, etc.)
5. Writes all rows to the configured Google Sheet

## Scheduling (Optional)

To auto-run weekly, add a cron job:

```bash
crontab -e
# Run every Monday at 11 PM
0 23 * * 1 /usr/bin/python3 /path/to/riot_tournament_stats.py
```

## Legal

This project uses the Riot Games API in compliance with [Riot's developer policies](https://developer.riotgames.com/policies/general). Riot Games does not endorse or sponsor this project.
