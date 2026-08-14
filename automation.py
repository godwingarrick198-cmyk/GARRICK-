import logging
import re
import threading
import time
from datetime import datetime, timedelta

from database import SessionLocal
from models import Campaign, Lead, LeadStatus
from osm import search_businesses
from analyze import analyze_site
from gemini import score
from telegram import get_updates, notify, send_message, is_channel_admin

logger = logging.getLogger(__name__)

_FIND_RE = re.compile(r"^/find(?:@[A-Za-z0-9_]+)?\s+(.+)$", re.I)


def _parse_find(text):
    match = _FIND_RE.match((text or "").strip())
    if not match:
        return None

    parts = [p.strip() for p in match.group(1).split("|")]
    if len(parts) != 4:
        raise ValueError("Use: /find niche | city | businesses_per_day | days")

    niche, city, per_day_text, days_text = parts
    if not 2 <= len(niche) <= 80:
        raise ValueError("Niche must be between 2 and 80 characters")
    if not 2 <= len(city) <= 120:
        raise ValueError("City must be between 2 and 120 characters")

    try:
        per_day = int(per_day_text)
        days = int(days_text)
    except ValueError as exc:
        raise ValueError("Businesses per day and days must be whole numbers") from exc

    if not 1 <= per_day <= 50:
        raise ValueError("Businesses per day must be between 1 and 50")
    if not 1 <= days <= 365:
        raise ValueError("Days must be between 1 and 365")

    return niche, city, per_day, days


def _chat_id_from_update(update):
    message = update.get("message") or update.get("channel_post")
    return str(message.get("chat", {}).get("id")) if message else None


def _text_from_update(update):
    message = update.get("message") or update.get("channel_post")
    return (message or {}).get("text")


def _handle_find(update):
    text = _text_from_update(update)
    if not text:
        return False

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return True

    try:
        parsed = _parse_find(text)
    except ValueError as exc:
        send_message(str(chat.get("id")), f"❌ Campaign command error\n\n{exc}")
        return True

    if parsed is None:
        return False

    sender = message.get("from") or {}
    sender_id = sender.get("id")
    private_chat_id = _chat_id_from_update(update)
    if not sender_id or not private_chat_id:
        return True

    # The reporting destination remains TELEGRAM_CHAT_ID (the channel).
    # Only a user who is an admin of that channel can control Garrick by DM.
    if not is_channel_admin(sender_id):
        logger.warning("Ignoring /find from a Telegram user who is not a channel admin")
        send_message(private_chat_id, "❌ You must be an admin of Garrick's reporting channel to create campaigns.")
        return True

    niche, city, per_day, days = parsed
    update_id = int(update.get("update_id"))
    now = datetime.utcnow()

    with SessionLocal() as db:
        existing = db.query(Campaign).filter_by(telegram_update_id=update_id).first()
        if existing:
            return True

        campaign = Campaign(
            niche=niche,
            city=city,
            leads_per_day=per_day,
            total_days=days,
            days_completed=0,
            next_run=now,
            status="ACTIVE",
            telegram_update_id=update_id,
        )
        db.add(campaign)
        db.commit()
        campaign_id = campaign.id

    send_message(
        private_chat_id,
        "✅ Campaign created\n\n"
        f"Niche: {niche}\n"
        f"City: {city}\n"
        f"Businesses/day: {per_day}\n"
        f"Days: {days}\n"
        f"Campaign ID: {campaign_id}\n\n"
        "Garrick will start the first run automatically.\n"
        "Qualified leads will be reported to your channel."
    )
    return True


def _process_lead(db, lead):
    if not lead.website:
        lead.status = LeadStatus.NO_WEBSITE
        db.commit()
        return

    if lead.status in (LeadStatus.ANALYZED, LeadStatus.QUALIFIED, LeadStatus.CONTACTED, LeadStatus.CLIENT):
        return

    try:
        site = analyze_site(lead.website)
        lead.website_score = site.get("website_score")
        lead.analysis = site

        ai = score(lead.to_dict(), site)
        lead.lead_score = ai.get("lead_score")
        lead.problems = ai.get("problems", [])
        lead.opportunity = ai.get("opportunity", "")
        lead.status = LeadStatus.QUALIFIED if lead.lead_score >= 80 else LeadStatus.ANALYZED
        db.commit()

        if lead.status == LeadStatus.QUALIFIED:
            try:
                notify(
                    "🔥 QUALIFIED LEAD\n\n"
                    f"{lead.name}\n"
                    f"City: {lead.city}\n"
                    f"Score: {lead.lead_score}/100\n"
                    f"Website: {lead.website}\n"
                    f"Opportunity: {lead.opportunity}\n"
                    f"Problems: {', '.join(lead.problems or [])}"
                )
            except Exception:
                logger.exception("Telegram notification failed for lead %s", lead.id)

    except Exception:
        db.rollback()
        logger.exception("Lead analysis failed for lead %s", lead.id)


def _run_campaign(campaign_id):
    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        if not campaign or campaign.status != "ACTIVE":
            return
        if campaign.next_run > datetime.utcnow():
            return

        logger.info(
            "Running campaign %s: day %s/%s",
            campaign.id,
            campaign.days_completed + 1,
            campaign.total_days,
        )

        try:
            # Ask OSM for a larger pool so existing source_id duplicates do not
            # consume the whole daily quota.
            pool_size = min(max(campaign.leads_per_day * 5, campaign.leads_per_day), 50)
            rows = search_businesses(campaign.niche, campaign.city, pool_size)

            selected = []
            for row in rows:
                existing = db.query(Lead).filter_by(source_id=row["source_id"]).first()
                if existing:
                    if existing.status == LeadStatus.NEW:
                        selected.append(existing)
                    continue

                lead = Lead(**row)
                db.add(lead)
                db.commit()
                db.refresh(lead)
                selected.append(lead)

                if len(selected) >= campaign.leads_per_day:
                    break

            for lead in selected:
                _process_lead(db, lead)

            campaign.days_completed += 1
            campaign.last_run_at = datetime.utcnow()
            campaign.last_error = None

            if campaign.days_completed >= campaign.total_days:
                campaign.status = "COMPLETED"
                campaign.next_run = campaign.last_run_at
            else:
                campaign.next_run = campaign.next_run + timedelta(days=1)

            db.commit()

            notify(
                "📊 Campaign update\n\n"
                f"Campaign {campaign.id}: {campaign.niche} in {campaign.city}\n"
                f"Completed day {campaign.days_completed}/{campaign.total_days}\n"
                f"New/processable businesses: {len(selected)}"
            )

        except Exception as exc:
            db.rollback()
            campaign = db.get(Campaign, campaign_id)
            if campaign:
                campaign.last_error = type(exc).__name__
                campaign.next_run = datetime.utcnow() + timedelta(hours=1)
                db.commit()
            logger.exception("Campaign %s failed; retrying later", campaign_id)


def _run_due_campaigns():
    now = datetime.utcnow()
    with SessionLocal() as db:
        campaigns = (
            db.query(Campaign)
            .filter(Campaign.status == "ACTIVE", Campaign.next_run <= now)
            .order_by(Campaign.next_run.asc())
            .all()
        )
        ids = [c.id for c in campaigns]

    for campaign_id in ids:
        _run_campaign(campaign_id)


def _loop():
    offset = None
    while True:
        try:
            updates = get_updates(offset=offset, timeout=20)
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    _handle_find(update)
                except Exception:
                    logger.exception("Telegram command processing failed")
        except Exception:
            logger.exception("Telegram polling cycle failed")
            time.sleep(10)

        try:
            _run_due_campaigns()
        except Exception:
            logger.exception("Due campaign processing failed")


def start_automation():
    thread = threading.Thread(target=_loop, name="garrick-automation", daemon=True)
    thread.start()
    logger.info("Garrick automation started")
