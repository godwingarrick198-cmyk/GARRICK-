import os
import logging

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)
from pydantic import BaseModel, Field

from database import (
    init_db,
    SessionLocal,
)

from models import (
    Lead,
    LeadStatus,
    Campaign,
)

from osm import search_businesses
from analyze import analyze_site
from gemini import score

from telegram import (
    notify,
    set_webhook,
    webhook_secret,
)

from automation import (
    start_automation,
    process_telegram_update,
)


logger = logging.getLogger(__name__)


app = FastAPI(
    title="Garrick AI Outreach",
    version="1.0.0",
)


class SearchRequest(BaseModel):
    niche: str = Field(
        min_length=2,
        max_length=80,
    )

    city: str = Field(
        min_length=2,
        max_length=120,
    )

    lead_count: int = Field(
        default=10,
        ge=1,
        le=50,
    )


@app.on_event("startup")
def startup():
    """
    Initialize the database, configure the Telegram
    webhook, and start the campaign scheduler.

    IMPORTANT:
    We do NOT start Telegram getUpdates polling.
    """

    init_db()

    public_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("GARRICK_PUBLIC_URL")
    )

    if public_url:
        try:
            webhook_url = set_webhook(
                public_url
            )

            print(
                f"Telegram webhook configured: "
                f"{webhook_url}"
            )

        except Exception:
            logger.exception(
                "Telegram webhook configuration failed"
            )

    else:
        logger.warning(
            "No public Render URL configured. "
            "Telegram webhook was not configured."
        )

    start_automation()


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
):
    """
    Receive Telegram updates through webhook.

    Telegram should receive HTTP 200 even when the
    internal campaign command fails, so Telegram does
    not repeatedly redeliver the same update.
    """

    expected = webhook_secret()

    received = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    # If a secret is configured, validate it.
    if expected:
        if received != expected:
            raise HTTPException(
                status_code=403,
                detail="Forbidden",
            )

    try:
        update = await request.json()

    except Exception:
        logger.exception(
            "Invalid Telegram webhook JSON"
        )

        # Still acknowledge Telegram.
        return {
            "ok": True,
            "handled": False,
        }

    try:
        process_telegram_update(
            update
        )

    except Exception:
        logger.exception(
            "Telegram webhook processing failed"
        )

    # Always acknowledge Telegram successfully.
    return {
        "ok": True,
        "handled": True,
    }


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "garrick-ai-outreach",
    }


@app.post("/api/search/businesses")
def search(req: SearchRequest):
    try:
        rows = search_businesses(
            req.niche,
            req.city,
            req.lead_count,
        )

    except Exception as exc:
        logger.exception(
            "Business search failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Business search failed: "
                f"{type(exc).__name__}"
            ),
        ) from exc

    saved = []

    with SessionLocal() as db:
        for row in rows:

            existing = (
                db.query(Lead)
                .filter_by(
                    source_id=row["source_id"]
                )
                .first()
            )

            if existing:
                continue

            try:
                lead = Lead(**row)

                db.add(lead)
                db.commit()
                db.refresh(lead)

                saved.append(lead)

            except Exception:
                db.rollback()

                logger.exception(
                    "Failed to save lead"
                )

    return {
        "count": len(saved),
        "leads": [
            lead.to_dict()
            for lead in saved
        ],
    }


@app.get("/api/leads")
def leads(limit: int = 50):
    limit = max(
        1,
        min(limit, 200),
    )

    with SessionLocal() as db:
        rows = (
            db.query(Lead)
            .order_by(
                Lead.lead_score.desc().nullslast()
            )
            .limit(limit)
            .all()
        )

        return {
            "leads": [
                row.to_dict()
                for row in rows
            ]
        }


@app.post("/api/analyze/website/{lead_id}")
def analyze(lead_id: int):
    with SessionLocal() as db:
        lead = db.get(
            Lead,
            lead_id,
        )

        if not lead:
            raise HTTPException(
                status_code=404,
                detail="Lead not found",
            )

        # No website is a valid prospect.
        if not lead.website:
            lead.status = LeadStatus.NO_WEBSITE

            db.commit()
            db.refresh(lead)

            try:
                notify(
                    "🚨 NO WEBSITE LEAD\n\n"
                    f"Business: {lead.name}\n"
                    f"City: {lead.city}\n"
                    f"Category: {lead.category}\n"
                    f"Phone: "
                    f"{lead.phone or 'Not available'}\n"
                    "Website: None\n"
                    "Status: NO_WEBSITE\n\n"
                    "💡 Opportunity: "
                    "Potential website prospect."
                )

            except Exception:
                logger.exception(
                    "No-website notification failed "
                    "for lead %s",
                    lead.id,
                )

            return lead.to_dict(True)

        try:
            site = analyze_site(
                lead.website
            )

            ai = score(
                lead.to_dict(),
                site,
            )

            lead.website_score = site.get(
                "website_score"
            )

            lead.analysis = site

            lead.lead_score = ai.get(
                "lead_score"
            )

            lead.problems = ai.get(
                "problems",
                [],
            )

            lead.opportunity = ai.get(
                "opportunity",
                "",
            )

            if (
                lead.lead_score is not None
                and lead.lead_score >= 80
            ):
                lead.status = LeadStatus.QUALIFIED
            else:
                lead.status = LeadStatus.ANALYZED

            db.commit()
            db.refresh(lead)

        except Exception as exc:
            db.rollback()

            logger.exception(
                "Website analysis failed "
                "for lead %s",
                lead.id,
            )

            raise HTTPException(
                status_code=502,
                detail=(
                    "Website analysis failed: "
                    f"{type(exc).__name__}"
                ),
            ) from exc

        if lead.status == LeadStatus.QUALIFIED:
            try:
                notify(
                    "🔥 QUALIFIED LEAD\n\n"
                    f"Business: {lead.name}\n"
                    f"City: {lead.city}\n"
                    f"Score: "
                    f"{lead.lead_score}/100\n"
                    f"Website: {lead.website}\n"
                    f"Opportunity: "
                    f"{lead.opportunity}\n"
                    f"Problems: "
                    f"{', '.join(lead.problems or [])}"
                )

            except Exception:
                logger.exception(
                    "Qualified-lead notification failed "
                    "for lead %s",
                    lead.id,
                )

        return lead.to_dict(True)


@app.get("/api/campaigns")
def campaigns(limit: int = 50):
    limit = max(
        1,
        min(limit, 200),
    )

    with SessionLocal() as db:
        rows = (
            db.query(Campaign)
            .order_by(
                Campaign.created_at.desc()
            )
            .limit(limit)
            .all()
        )

        return {
            "campaigns": [
                {
                    "id": campaign.id,
                    "niche": campaign.niche,
                    "city": campaign.city,
                    "leads_per_day":
                        campaign.leads_per_day,
                    "total_days":
                        campaign.total_days,
                    "days_completed":
                        campaign.days_completed,
                    "next_run":
                        (
                            campaign.next_run.isoformat()
                            if campaign.next_run
                            else None
                        ),
                    "status":
                        campaign.status,
                    "created_at":
                        (
                            campaign.created_at.isoformat()
                            if campaign.created_at
                            else None
                        ),
                    "last_run_at":
                        (
                            campaign.last_run_at.isoformat()
                            if campaign.last_run_at
                            else None
                        ),
                    "last_error":
                        campaign.last_error,
                }
                for campaign in rows
            ]
        }
