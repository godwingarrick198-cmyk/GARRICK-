# Garrick AI Outreach
Free-first lead discovery and qualification.

Flow: OpenStreetMap/Overpass → website analysis → Gemini → SQLite → Telegram.

## Required environment
GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

No Google Places API is required.

## Run
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload

Docs: http://127.0.0.1:8000/docs

Search: POST /api/search/businesses
{"niche":"dentists","city":"Miami","lead_count":5}

Then analyze: POST /api/analyze/website/{lead_id}

OpenStreetMap coverage varies by location. Respect public Overpass/Nominatim limits.
