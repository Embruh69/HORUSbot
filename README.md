# Lancer Discord Bot

A Discord bot for the **Lancer TTRPG**, modelled after Avrae for D&D.  
Import your pilot from **comp/con** and view a full character sheet right in Discord.

---

## Setup

### 1 — Prerequisites
- Python 3.11+
- A Discord bot token ([create one here](https://discord.com/developers/applications))

### 2 — Install dependencies
```bash
pip install -r requirements.txt
```

### 3 — Configure
Create a `.env` file in the project root:
```
DISCORD_TOKEN=your_bot_token_here
```

### 4 — Run
```bash
python bot.py
```

---

## Commands

| Command | Aliases | Description |
|---|---|---|
| `!import` | `!i` | Attach your comp/con JSON and run this to import your pilot |
| `!sheet` | `!s` | Summary view (pilot + active mech) |
| `!sheet pilot` | `!s p` | Full pilot stats, skills, talents, licenses |
| `!sheet mech` | `!s m` | Active mech stats with HP/Heat bars |
| `!sheet weapons` | `!s w` | Weapon details |
| `!sheet systems` | `!s sys` | Installed systems |
| `!sheet talents` | `!s t` | Talent descriptions |
| `!delete` | `!remove` | Remove your character from this server |
| `!lancer` | — | Quick-start help embed |

---

## How to export from comp/con

1. Open your pilot in **comp/con**
2. Click the **cloud/export** icon → **Export → Save Pilot**
3. Save the `.json` file
4. In Discord, attach the file to a message and run `!import`

---

## Project structure

```
lancer-bot/
├── bot.py                  # Entry point
├── requirements.txt
├── .env                    # Your token (never commit this)
├── cogs/
│   └── character.py        # All character commands
└── utils/
    ├── parser.py            # comp/con JSON → dataclasses
    ├── embeds.py            # Discord embed builders
    └── storage.py           # In-memory character store
```
