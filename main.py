import os

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from database import init_db, SessionLocal
from models import Lead, LeadStatus, Campaign
from osm import search_businesses
from analyze import analyze_site
from gemini import score
from telegram import notify, set_webhook, webhook_secret
from automation import start_automation, process_telegram_update

app = FastAPI(title="Garrick AI Outreach", version="1.0.0")


class SearchRequest(BaseModel):
    niche: str = Field(min_length=2, max_length=80)
    city: str = Field(min_length=2, max_length=120)
    lead_count: int = Field(default=10, ge=1, le=50)


@app.on_event("startup")
def startup():
    init_db()

    public_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("GARRICK_PUBLIC_URL")
    if public_url:
        try:
            webhook_url = set_webhook(public_url)
            print(f"Telegram webhook configured: {webhook_url}")
        except Exception:
            # The app can still serve the API/campaign scheduler if Telegram webhook setup fails.
            print("Telegram webhook configuration failed")

    start_automation()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    expected = webhook_secret()
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if received != expected:
        raise HTTPException(403, "Forbidden")

    update = await request.json()
    process_telegram_update(update)
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "garrick-ai-outreach"}


@app.post("/api/search/businesses")
def search(req: SearchRequest):
    try:
        rows = search_businesses(req.niche, req.city, req.lead_count)
    except Exception as exc:
        raise HTTPException(502, f"Business search failed: {type(exc).__name__}") from exc

    saved = []
    with SessionLocal() as db:
        for row in rows:
            if db.query(Lead).filter_by(source_id=row["source_id"]).first():
                continue
            lead = Lead(**row)
            db.add(lead)
            saved.append(lead)
        db.commit()
        for lead in saved:
            db.refresh(lead)

    return {"count": len(saved), "leads": [lead.to_dict() for lead in saved]}


@app.get("/api/leads")
def leads(limit: int = 50):
    with SessionLocal() as db:
        rows = (
            db.query(Lead)
            .order_by(Lead.lead_score.desc().nullslast())
            .limit(limit)
            .all()
        )
        return {"leads": [row.to_dict() for row in rows]}


@app.post("/api/analyze/website/{lead_id}")
def analyze(lead_id: int):
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404, "Lead not found")
        if not lead.website:
            lead.status = LeadStatus.NO_WEBSITE
            db.commit()
            return lead.to_dict()

        site = analyze_site(lead.website)
        ai = score(lead.to_dict(), site)
        lead.website_score = site["website_score"]
        lead.analysis = site
        lead.lead_score = ai["lead_score"]
        lead.problems = ai.get("problems", [])
        lead.opportunity = ai.get("opportunity", "")
        lead.status = LeadStatus.QUALIFIED if lead.lead_score >= 80 else LeadStatus.ANALYZED
        db.commit()
        db.refresh(lead)

        if lead.status == LeadStatus.QUALIFIED:
            try:
                notify(
                    "🔥 QUALIFIED LEAD\n\n"
                    f"{lead.name}\n"
                    f"City: {lead.city}\n"
                    f"Score: {lead.lead_score}/100\n"
                    f"Website: {lead.website}\n"
                    f"{lead.opportunity}\n"
                    f"Problems: {', '.join(lead.problems or [])}"
                )
            except Exception:
                pass

        return lead.to_dict(True)


@app.get("/api/campaigns")
def campaigns(limit: int = 50):
    with SessionLocal() as db:
        rows = db.query(Campaign).order_by(Campaign.created_at.desc()).limit(limit).all()
        return {
            "campaigns": [
                {
                    "id": c.id,
                    "niche": c.niche,
                    "city": c.city,
                    "leads_per_day": c.leads_per_day,
                    "total_days": c.total_days,
                    "days_completed": c.days_completed,
                    "next_run": c.next_run.isoformat() if c.next_run else None,
                    "status": c.status,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "last_run_at": c.last_run_at.isoformat() if c.last_run_at else None,
                    "last_error": c.last_error,
                }
                for c in rows
            ]
            }
    
