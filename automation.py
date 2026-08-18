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
from telegram import notify, send_message, is_channel_admin

logger = logging.getLogger(__name__)

_FIND_RE = re.compile(r"^/find(?:@[A-Za-z0-9_]+)?\s+(.+)$", re.I)
_COMMAND_RE = re.compile(r"^/(campaigns?|pause|resume|stop|run)(?:@[A-Za-z0-9_]+)?(?:\s+(\d+))?\s*$", re.I)

_AUTOMATION_STARTED = False
_AUTOMATION_LOCK = threading.Lock()
_CAMPAIGN_LOCKS = {}
_CAMPAIGN_LOCKS_GUARD = threading.Lock()


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
    if not message:
        return None
    chat_id = (message.get("chat") or {}).get("id")
    return str(chat_id) if chat_id is not None else None


def _text_from_update(update):
    message = update.get("message") or update.get("channel_post")
    return (message or {}).get("text")


def _admin_required(message):
    sender = message.get("from") or {}
    sender_id = sender.get("id")
    return bool(sender_id and is_channel_admin(sender_id))


def _campaign_summary(campaign):
    next_run = campaign.next_run.isoformat(sep=" ") if campaign.next_run else "None"
    return (
        f"Campaign {campaign.id}\n"
        f"Niche: {campaign.niche}\n"
        f"City: {campaign.city}\n"
        f"Businesses/day: {campaign.leads_per_day}\n"
        f"Progress: {campaign.days_completed}/{campaign.total_days} days\n"
        f"Status: {campaign.status}\n"
        f"Next run (UTC): {next_run}\n"
        f"Last run (UTC): {campaign.last_run_at.isoformat(sep=' ') if campaign.last_run_at else 'Never'}\n"
        f"Last error: {campaign.last_error or 'None'}"
    )


def _handle_management(message, text, private_chat_id):
    match = _COMMAND_RE.match((text or "").strip())
    if not match:
        return False

    command = match.group(1).lower()
    campaign_id_text = match.group(2)

    if command == "campaigns":
        with SessionLocal() as db:
            campaigns = db.query(Campaign).order_by(Campaign.created_at.desc()).limit(20).all()
            if not campaigns:
                send_message(private_chat_id, "📋 No campaigns found.")
                return True
            lines = ["📋 CAMPAIGNS\n"]
            for c in campaigns:
                lines.append(
                    f"#{c.id} — {c.niche} in {c.city}\n"
                    f"{c.days_completed}/{c.total_days} days — {c.status}\n"
                    f"Next: {c.next_run.isoformat(sep=' ') if c.next_run else 'None'} UTC\n"
                )
            send_message(private_chat_id, "\n".join(lines))
        return True

    if not campaign_id_text:
        send_message(private_chat_id, "Use: /campaign 1, /pause 1, /resume 1, /run 1, or /stop 1")
        return True

    campaign_id = int(campaign_id_text)

    with SessionLocal() as db:
        campaign = db.get(Campaign, campaign_id)
        if not campaign:
            send_message(private_chat_id, f"❌ Campaign {campaign_id} was not found.")
            return True

        if command == "campaign":
            send_message(private_chat_id, "📌 " + _campaign_summary(campaign))
            return True

        if command == "pause":
            if campaign.status == "COMPLETED":
                send_message(private_chat_id, "❌ That campaign is already completed.")
                return True
            campaign.status = "PAUSED"
            db.commit()
            send_message(private_chat_id, f"⏸️ Campaign {campaign.id} paused.")
            return True

        if command == "resume":
            if campaign.status == "COMPLETED":
                send_message(private_chat_id, "❌ A completed campaign cannot be resumed.")
                return True
            campaign.status = "ACTIVE"
            if not campaign.next_run or campaign.next_run > datetime.utcnow():
                campaign.next_run = datetime.utcnow()
            campaign.last_error = None
            db.commit()
            send_message(private_chat_id, f"▶️ Campaign {campaign.id} resumed. Garrick will process it when due.")
            campaign_id_to_run = campaign.id
        elif command == "run":
            if campaign.status != "ACTIVE":
                send_message(private_chat_id, f"❌ Campaign {campaign.id} is {campaign.status}. Resume it first.")
                return True
            campaign.next_run = datetime.utcnow()
            db.commit()
            send_message(private_chat_id, f"🚀 Campaign {campaign.id} queued for an immediate run.")
            campaign_id_to_run = campaign.id
        elif command == "stop":
            if campaign.status == "COMPLETED":
                send_message(private_chat_id, "ℹ️ That campaign is already completed.")
                return True
            campaign.status = "STOPPED"
            campaign.last_error = None
            db.commit()
            send_message(private_chat_id, f"🛑 Campaign {campaign.id} stopped. You can use /resume {campaign.id} if you want to continue it later.")
            return True
        else:
            return True

    threading.Thread(
        target=_run_campaign,
        args=(campaign_id_to_run,),
        name=f"campaign-{campaign_id_to_run}",
        daemon=True,
    ).start()
    return True


def _handle_find(update):
    text = _text_from_update(update)
    if not text:
        return False
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private":
        return True

    private_chat_id = _chat_id_from_update(update)
    if not private_chat_id:
        return True

    if not _admin_required(message):
        send_message(private_chat_id, "❌ You are not authorized to manage Garrick campaigns.")
        return True

    try:
        parsed = _parse_find(text)
    except ValueError as exc:
        send_message(private_chat_id, f"❌ Campaign command error\n\n{exc}")
        return True

    if parsed is None:
        return _handle_management(message, text, private_chat_id)

    niche, city, per_day, days = parsed
    update_id = update.get("update_id")
    if update_id is None:
        return True

    now = datetime.utcnow()
    with SessionLocal() as db:
        existing = db.query(Campaign).filter_by(telegram_update_id=int(update_id)).first()
        if existing:
            send_message(private_chat_id, f"ℹ️ This Telegram update already created Campaign {existing.id}.")
            return True
        campaign = Campaign(
            niche=niche,
            city=city,
            leads_per_day=per_day,
            total_days=days,
            days_completed=0,
            next_run=now,
            status="ACTIVE",
            telegram_update_id=int(update_id),
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
        "Garrick will run the first search now.\n"
        "Use /campaigns to manage your campaigns.\n"
        "Qualified and no-website leads will be reported to your channel.",
    )
    threading.Thread(
        target=_run_campaign,
        args=(campaign_id,),
        name=f"campaign-{campaign_id}",
        daemon=True,
    ).start()
    return True


def process_telegram_update(update):
    """Process one Telegram webhook update. Returns True when handled."""
    try:
        return _handle_find(update)
    except Exception:
        logger.exception("Telegram command processing failed")
        return False


def _process_lead(db, lead):
    if not lead.website:
        lead.status = LeadStatus.NO_WEBSITE
        db.commit()
        try:
            notify(
                "🚨 NO WEBSITE LEAD\n\n"
                f"Business: {lead.name}\n"
                f"City: {lead.city}\n"
                f"Category: {lead.category}\n"
                f"Phone: {lead.phone or 'Not available'}\n"
                "Website: None\n"
                "Status: NO_WEBSITE\n\n"
                "💡 Opportunity: This business may be a potential website prospect."
            )
        except Exception:
            logger.exception("Telegram notification failed for no-website lead %s", lead.id)
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
        lead.status = LeadStatus.QUALIFIED if lead.lead_score is not None and lead.lead_score >= 80 else LeadStatus.ANALYZED
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Lead analysis failed for lead %s", lead.id)
        return

    if lead.status == LeadStatus.QUALIFIED:
        try:
            notify(
                "🔥 QUALIFIED LEAD\n\n"
                f"Business: {lead.name}\n"
                f"City: {lead.city}\n"
                f"Score: {lead.lead_score}/100\n"
                f"Website: {lead.website}\n"
                f"Opportunity: {lead.opportunity}\n"
                f"Problems: {', '.join(lead.problems or [])}"
            )
        except Exception:
            logger.exception("Telegram notification failed for qualified lead %s", lead.id)


def _campaign_lock(campaign_id):
    with _CAMPAIGN_LOCKS_GUARD:
        lock = _CAMPAIGN_LOCKS.get(campaign_id)
        if lock is None:
            lock = threading.Lock()
            _CAMPAIGN_LOCKS[campaign_id] = lock
        return lock


def _recover_interrupted_campaigns():
    """Return campaigns left RUNNING by a crashed/restarted process to ACTIVE."""
    with SessionLocal() as db:
        rows = db.query(Campaign).filter(Campaign.status == "RUNNING").all()
        for campaign in rows:
            campaign.status = "ACTIVE"
            if not campaign.next_run or campaign.next_run > datetime.utcnow():
                campaign.next_run = datetime.utcnow()
        if rows:
            db.commit()
            logger.warning("Recovered %s interrupted campaign(s)", len(rows))


def _run_campaign(campaign_id):
    lock = _campaign_lock(campaign_id)
    if not lock.acquire(blocking=False):
        logger.info("Campaign %s is already running", campaign_id)
        return

    try:
        with SessionLocal() as db:
            campaign = db.get(Campaign, campaign_id)
            if not campaign or campaign.status != "ACTIVE":
                return

            now = datetime.utcnow()
            if campaign.next_run and campaign.next_run > now:
                return

            # Claim the campaign in the database so another process/thread
            # cannot execute the same campaign day at the same time.
            campaign.status = "RUNNING"
            db.commit()

            logger.info(
                "Running campaign %s: day %s/%s",
                campaign.id,
                campaign.days_completed + 1,
                campaign.total_days,
            )

            try:
                pool_size = min(max(campaign.leads_per_day * 10, 50), 200)
                rows = search_businesses(campaign.niche, campaign.city, pool_size)
                selected = []

                for row in rows:
                    source_id = row.get("source_id")
                    if not source_id:
                        continue
                    if db.query(Lead).filter_by(source_id=source_id).first():
                        continue

                    lead = Lead(**row)
                    db.add(lead)
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                        continue
                    db.refresh(lead)
                    selected.append(lead)
                    if len(selected) >= campaign.leads_per_day:
                        break

                for lead in selected:
                    _process_lead(db, lead)

                completed_at = datetime.utcnow()
                campaign.days_completed += 1
                campaign.last_run_at = completed_at
                campaign.last_error = None

                if campaign.days_completed >= campaign.total_days:
                    campaign.status = "COMPLETED"
                    campaign.next_run = completed_at
                else:
                    # Keep the original schedule. If several days were missed
                    # while Render was asleep, the next loop can catch them up.
                    campaign.status = "ACTIVE"
                    campaign.next_run = campaign.next_run + timedelta(days=1)

                db.commit()

                try:
                    notify(
                        "📊 Campaign update\n\n"
                        f"Campaign {campaign.id}: {campaign.niche} in {campaign.city}\n"
                        f"Completed day {campaign.days_completed}/{campaign.total_days}\n"
                        f"New businesses processed: {len(selected)}\n"
                        f"Next run (UTC): {campaign.next_run.isoformat(sep=' ') if campaign.status == 'ACTIVE' else 'Completed'}"
                    )
                except Exception:
                    logger.exception("Campaign notification failed for campaign %s", campaign.id)

            except Exception as exc:
                db.rollback()
                campaign = db.get(Campaign, campaign_id)
                if campaign and campaign.status == "RUNNING":
                    campaign.status = "ACTIVE"
                    campaign.last_error = type(exc).__name__
                    campaign.next_run = datetime.utcnow() + timedelta(minutes=30)
                    db.commit()
                logger.exception("Campaign %s failed; retrying later", campaign_id)
    finally:
        lock.release()


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
    # No Telegram getUpdates polling here. Telegram commands arrive through
    # the FastAPI webhook, which prevents the 409 competing-poller conflict.
    while True:
        try:
            _run_due_campaigns()
        except Exception:
            logger.exception("Due campaign processing failed")
        time.sleep(60)


def start_automation():
    global _AUTOMATION_STARTED
    with _AUTOMATION_LOCK:
        if _AUTOMATION_STARTED:
            logger.info("Garrick automation already started")
            return
        _AUTOMATION_STARTED = True

    # Recover campaigns interrupted by a deploy/restart, then catch up.
    try:
        _recover_interrupted_campaigns()
    except Exception:
        logger.exception("Interrupted campaign recovery failed")

    try:
        _run_due_campaigns()
    except Exception:
        logger.exception("Startup campaign catch-up failed")

    thread = threading.Thread(target=_loop, name="garrick-automation", daemon=True)
    thread.start()
    logger.info("Garrick automation started")
