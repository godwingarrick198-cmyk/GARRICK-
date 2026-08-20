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


_FIND_RE = re.compile(
    r"^/find(?:@[A-Za-z0-9_]+)?\s+(.+)$",
    re.I,
)

_COMMAND_RE = re.compile(
    r"^/(campaigns?|pause|resume|stop|run)"
    r"(?:@[A-Za-z0-9_]+)?"
    r"(?:\s+(\d+))?\s*$",
    re.I,
)


_AUTOMATION_STARTED = False
_AUTOMATION_LOCK = threading.Lock()

_CAMPAIGN_LOCKS = {}
_CAMPAIGN_LOCKS_GUARD = threading.Lock()


# Campaign search retry settings.
SEARCH_RETRIES = 3
SEARCH_RETRY_DELAY_SECONDS = 60

# Scheduler interval.
SCHEDULER_INTERVAL_SECONDS = 30


def _utcnow():
    return datetime.utcnow()


def _parse_find(text):
    match = _FIND_RE.match((text or "").strip())

    if not match:
        return None

    parts = [
        p.strip()
        for p in match.group(1).split("|")
    ]

    if len(parts) != 4:
        raise ValueError(
            "Use:\n"
            "/find niche | city | businesses_per_day | days"
        )

    niche, city, per_day_text, days_text = parts

    if not 2 <= len(niche) <= 80:
        raise ValueError(
            "Niche must be between 2 and 80 characters."
        )

    if not 2 <= len(city) <= 120:
        raise ValueError(
            "City must be between 2 and 120 characters."
        )

    try:
        per_day = int(per_day_text)
        days = int(days_text)
    except ValueError as exc:
        raise ValueError(
            "Businesses per day and days must be whole numbers."
        ) from exc

    if not 1 <= per_day <= 50:
        raise ValueError(
            "Businesses per day must be between 1 and 50."
        )

    if not 1 <= days <= 365:
        raise ValueError(
            "Days must be between 1 and 365."
        )

    return niche, city, per_day, days


def _chat_id_from_update(update):
    message = (
        update.get("message")
        or update.get("channel_post")
    )

    if not message:
        return None

    chat_id = (message.get("chat") or {}).get("id")

    if chat_id is None:
        return None

    return str(chat_id)


def _text_from_update(update):
    message = (
        update.get("message")
        or update.get("channel_post")
    )

    return (message or {}).get("text")


def _admin_required(message):
    sender = message.get("from") or {}
    sender_id = sender.get("id")

    if not sender_id:
        return False

    return bool(is_channel_admin(sender_id))


def _campaign_summary(campaign):
    next_run = (
        campaign.next_run.isoformat(sep=" ")
        if campaign.next_run
        else "None"
    )

    last_run = (
        campaign.last_run_at.isoformat(sep=" ")
        if campaign.last_run_at
        else "Never"
    )

    return (
        f"📌 Campaign {campaign.id}\n\n"
        f"Niche: {campaign.niche}\n"
        f"City: {campaign.city}\n"
        f"Businesses/day: {campaign.leads_per_day}\n"
        f"Progress: "
        f"{campaign.days_completed}/{campaign.total_days} days\n"
        f"Status: {campaign.status}\n"
        f"Next run (UTC): {next_run}\n"
        f"Last run (UTC): {last_run}\n"
        f"Last error: "
        f"{campaign.last_error or 'None'}"
    )


def _send_private(chat_id, text):
    try:
        send_message(chat_id, text)
    except Exception:
        logger.exception(
            "Failed to send Telegram private message."
        )


def _handle_management(message, text, private_chat_id):
    match = _COMMAND_RE.match(
        (text or "").strip()
    )

    if not match:
        return False

    command = match.group(1).lower()
    campaign_id_text = match.group(2)

    # /campaigns
    if command == "campaigns":
        with SessionLocal() as db:
            rows = (
                db.query(Campaign)
                .order_by(Campaign.created_at.desc())
                .limit(20)
                .all()
            )

            if not rows:
                _send_private(
                    private_chat_id,
                    "📋 No campaigns found.",
                )
                return True

            lines = ["📋 CAMPAIGNS\n"]

            for campaign in rows:
                next_run = (
                    campaign.next_run.isoformat(sep=" ")
                    if campaign.next_run
                    else "None"
                )

                lines.append(
                    f"#{campaign.id} — "
                    f"{campaign.niche} in {campaign.city}\n"
                    f"Progress: "
                    f"{campaign.days_completed}/"
                    f"{campaign.total_days} days\n"
                    f"Status: {campaign.status}\n"
                    f"Next: {next_run} UTC\n"
                )

            _send_private(
                private_chat_id,
                "\n".join(lines),
            )

        return True

    if not campaign_id_text:
        _send_private(
            private_chat_id,
            "Use:\n"
            "/campaign 1\n"
            "/pause 1\n"
            "/resume 1\n"
            "/run 1\n"
            "/stop 1",
        )
        return True

    campaign_id = int(campaign_id_text)

    campaign_id_to_run = None

    with SessionLocal() as db:
        campaign = db.get(
            Campaign,
            campaign_id,
        )

        if not campaign:
            _send_private(
                private_chat_id,
                f"❌ Campaign {campaign_id} was not found.",
            )
            return True

        # /campaign 1
        if command == "campaign":
            _send_private(
                private_chat_id,
                _campaign_summary(campaign),
            )
            return True

        # /pause 1
        if command == "pause":
            if campaign.status == "COMPLETED":
                _send_private(
                    private_chat_id,
                    "❌ That campaign is already completed.",
                )
                return True

            campaign.status = "PAUSED"
            db.commit()

            _send_private(
                private_chat_id,
                f"⏸️ Campaign {campaign.id} paused.",
            )

            return True

        # /resume 1
        if command == "resume":
            if campaign.status == "COMPLETED":
                _send_private(
                    private_chat_id,
                    "❌ A completed campaign cannot be resumed.",
                )
                return True

            campaign.status = "ACTIVE"

            # Run immediately when resumed.
            campaign.next_run = _utcnow()
            campaign.last_error = None

            db.commit()

            campaign_id_to_run = campaign.id

            _send_private(
                private_chat_id,
                f"▶️ Campaign {campaign.id} resumed.\n"
                "Garrick will run it now.",
            )

        # /run 1
        elif command == "run":
            if campaign.status == "COMPLETED":
                _send_private(
                    private_chat_id,
                    "❌ That campaign is already completed.",
                )
                return True

            if campaign.status == "STOPPED":
                _send_private(
                    private_chat_id,
                    "❌ That campaign is stopped. "
                    f"Use /resume {campaign.id} first.",
                )
                return True

            campaign.status = "ACTIVE"
            campaign.next_run = _utcnow()
            campaign.last_error = None

            db.commit()

            campaign_id_to_run = campaign.id

            _send_private(
                private_chat_id,
                f"🚀 Campaign {campaign.id} "
                "queued for an immediate run.",
            )

        # /stop 1
        elif command == "stop":
            if campaign.status == "COMPLETED":
                _send_private(
                    private_chat_id,
                    "ℹ️ That campaign is already completed.",
                )
                return True

            campaign.status = "STOPPED"
            campaign.last_error = None
            db.commit()

            _send_private(
                private_chat_id,
                f"🛑 Campaign {campaign.id} stopped.\n\n"
                f"Use /resume {campaign.id} "
                "to continue it later.",
            )

            return True

        else:
            return True

    if campaign_id_to_run is not None:
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

    # Campaign commands must come from a private chat.
    if chat.get("type") != "private":
        return True

    private_chat_id = _chat_id_from_update(update)

    if not private_chat_id:
        return True

    if not _admin_required(message):
        _send_private(
            private_chat_id,
            "❌ You are not authorized "
            "to manage Garrick campaigns.",
        )
        return True

    # Management commands.
    if not text.lower().startswith("/find"):
        return _handle_management(
            message,
            text,
            private_chat_id,
        )

    try:
        parsed = _parse_find(text)

    except ValueError as exc:
        _send_private(
            private_chat_id,
            f"❌ Campaign command error\n\n{exc}",
        )
        return True

    if parsed is None:
        return False

    niche, city, per_day, days = parsed

    update_id = update.get("update_id")

    if update_id is None:
        logger.warning(
            "Telegram update had no update_id."
        )
        return True

    now = _utcnow()

    with SessionLocal() as db:
        existing = (
            db.query(Campaign)
            .filter_by(
                telegram_update_id=int(update_id)
            )
            .first()
        )

        if existing:
            _send_private(
                private_chat_id,
                f"ℹ️ This Telegram update already "
                f"created Campaign {existing.id}.",
            )
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
        db.refresh(campaign)

        campaign_id = campaign.id

    _send_private(
        private_chat_id,
        "✅ CAMPAIGN CREATED\n\n"
        f"Niche: {niche}\n"
        f"City: {city}\n"
        f"Businesses/day: {per_day}\n"
        f"Days: {days}\n"
        f"Campaign ID: {campaign_id}\n\n"
        "🔎 First search is starting now.\n"
        "🌐 Website leads will be analyzed.\n"
        "🚨 Businesses without websites "
        "will also be reported.\n\n"
        "Use /campaigns to manage campaigns.",
    )

    threading.Thread(
        target=_run_campaign,
        args=(campaign_id,),
        name=f"campaign-{campaign_id}",
        daemon=True,
    ).start()

    return True


def process_telegram_update(update):
    """
    Process one Telegram webhook update.

    IMPORTANT:
    This application uses Telegram webhooks.
    It does NOT use getUpdates polling.
    """

    try:
        return _handle_find(update)

    except Exception:
        logger.exception(
            "Telegram command processing failed"
        )
        return False


def _process_lead(db, lead):
    """
    Process a single lead.

    Leads without websites are valid prospects and
    are reported instead of being discarded.
    """

    if not lead.website:
        lead.status = LeadStatus.NO_WEBSITE

        db.commit()

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
                "This business may be a potential "
                "website prospect."
            )

        except Exception:
            logger.exception(
                "Telegram notification failed "
                "for no-website lead %s",
                lead.id,
            )

        return

    # Don't repeatedly analyze an already processed lead.
    if lead.status in (
        LeadStatus.ANALYZED,
        LeadStatus.QUALIFIED,
        LeadStatus.CONTACTED,
        LeadStatus.CLIENT,
    ):
        return

    try:
        site = analyze_site(lead.website)

        lead.website_score = site.get(
            "website_score"
        )

        lead.analysis = site

        ai = score(
            lead.to_dict(),
            site,
        )

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

    except Exception:
        db.rollback()

        logger.exception(
            "Lead analysis failed for lead %s",
            lead.id,
        )

        return

    if lead.status == LeadStatus.QUALIFIED:
        try:
            notify(
                "🔥 QUALIFIED LEAD\n\n"
                f"Business: {lead.name}\n"
                f"City: {lead.city}\n"
                f"Score: {lead.lead_score}/100\n"
                f"Website: {lead.website}\n"
                f"Opportunity: "
                f"{lead.opportunity}\n"
                f"Problems: "
                f"{', '.join(lead.problems or [])}"
            )

        except Exception:
            logger.exception(
                "Telegram notification failed "
                "for qualified lead %s",
                lead.id,
            )


def _campaign_lock(campaign_id):
    with _CAMPAIGN_LOCKS_GUARD:
        lock = _CAMPAIGN_LOCKS.get(
            campaign_id
        )

        if lock is None:
            lock = threading.Lock()
            _CAMPAIGN_LOCKS[
                campaign_id
            ] = lock

        return lock


def _recover_interrupted_campaigns():
    """
    Recover campaigns that were RUNNING when Render
    restarted or redeployed.
    """

    with SessionLocal() as db:
        rows = (
            db.query(Campaign)
            .filter(
                Campaign.status == "RUNNING"
            )
            .all()
        )

        for campaign in rows:
            campaign.status = "ACTIVE"
            campaign.next_run = _utcnow()
            campaign.last_error = (
                "Recovered after application restart"
            )

        if rows:
            db.commit()

            logger.warning(
                "Recovered %s interrupted campaign(s)",
                len(rows),
            )


def _search_with_retries(campaign, pool_size):
    """
    Search businesses with multiple attempts.

    This prevents one Overpass 504 from permanently
    killing the campaign day.
    """

    last_error = None

    for attempt in range(
        1,
        SEARCH_RETRIES + 1,
    ):
        try:
            logger.info(
                "Campaign %s business search "
                "attempt %s/%s",
                campaign.id,
                attempt,
                SEARCH_RETRIES,
            )

            rows = search_businesses(
                campaign.niche,
                campaign.city,
                pool_size,
            )

            if rows:
                return rows

            logger.warning(
                "Campaign %s search returned "
                "zero businesses.",
                campaign.id,
            )

            return []

        except Exception as exc:
            last_error = exc

            logger.warning(
                "Campaign %s search attempt "
                "%s failed: %s",
                campaign.id,
                attempt,
                exc,
            )

            if attempt < SEARCH_RETRIES:
                time.sleep(
                    SEARCH_RETRY_DELAY_SECONDS
                )

    raise RuntimeError(
        "Business search failed after "
        f"{SEARCH_RETRIES} attempts: "
        f"{type(last_error).__name__}"
    ) from last_error


def _run_campaign(campaign_id):
    lock = _campaign_lock(campaign_id)

    if not lock.acquire(blocking=False):
        logger.info(
            "Campaign %s is already running",
            campaign_id,
        )
        return

    try:
        with SessionLocal() as db:
            campaign = db.get(
                Campaign,
                campaign_id,
            )

            if not campaign:
                logger.warning(
                    "Campaign %s not found",
                    campaign_id,
                )
                return

            if campaign.status != "ACTIVE":
                return

            now = _utcnow()

            if (
                campaign.next_run
                and campaign.next_run > now
            ):
                return

            campaign.status = "RUNNING"
            campaign.last_error = None

            db.commit()

            current_day = (
                campaign.days_completed + 1
            )

            logger.info(
                "Running campaign %s: "
                "day %s/%s",
                campaign.id,
                current_day,
                campaign.total_days,
            )

            try:
                # Search a larger pool than the requested
                # daily number so duplicate businesses can
                # be filtered out.
                pool_size = min(
                    max(
                        campaign.leads_per_day * 10,
                        50,
                    ),
                    200,
                )

                rows = _search_with_retries(
                    campaign,
                    pool_size,
                )

                selected = []

                for row in rows:
                    source_id = row.get(
                        "source_id"
                    )

                    if not source_id:
                        continue

                    existing = (
                        db.query(Lead)
                        .filter_by(
                            source_id=source_id
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

                        selected.append(lead)

                    except Exception:
                        db.rollback()

                        logger.exception(
                            "Failed to save business "
                            "from campaign %s",
                            campaign.id,
                        )

                        continue

                    if (
                        len(selected)
                        >= campaign.leads_per_day
                    ):
                        break

                # Analyze/report selected leads.
                for lead in selected:
                    _process_lead(
                        db,
                        lead,
                    )

                completed_at = _utcnow()

                campaign.days_completed += 1
                campaign.last_run_at = completed_at
                campaign.last_error = None

                if (
                    campaign.days_completed
                    >= campaign.total_days
                ):
                    campaign.status = "COMPLETED"
                    campaign.next_run = completed_at

                else:
                    # Schedule the next campaign day
                    # 24 hours after THIS successful run.
                    campaign.status = "ACTIVE"

                    campaign.next_run = (
                        completed_at
                        + timedelta(days=1)
                    )

                db.commit()

                try:
                    next_run_text = (
                        campaign.next_run.isoformat(
                            sep=" "
                        )
                        if campaign.status == "ACTIVE"
                        else "Completed"
                    )

                    notify(
                        "📊 Campaign update\n\n"
                        f"Campaign {campaign.id}: "
                        f"{campaign.niche} in "
                        f"{campaign.city}\n"
                        f"Completed day "
                        f"{campaign.days_completed}/"
                        f"{campaign.total_days}\n"
                        f"New businesses processed: "
                        f"{len(selected)}\n"
                        f"Next run (UTC): "
                        f"{next_run_text}"
                    )

                except Exception:
                    logger.exception(
                        "Campaign notification failed "
                        "for campaign %s",
                        campaign.id,
                    )

            except Exception as exc:
                db.rollback()

                campaign = db.get(
                    Campaign,
                    campaign_id,
                )

                if campaign:
                    campaign.status = "ACTIVE"

                    campaign.last_error = (
                        f"{type(exc).__name__}: "
                        f"{str(exc)[:250]}"
                    )

                    # Retry failed campaigns sooner instead
                    # of waiting for the next day.
                    campaign.next_run = (
                        _utcnow()
                        + timedelta(minutes=10)
                    )

                    db.commit()

                logger.exception(
                    "Campaign %s failed; "
                    "scheduled retry.",
                    campaign_id,
                )

                try:
                    notify(
                        "⚠️ CAMPAIGN RETRY\n\n"
                        f"Campaign {campaign_id} "
                        "could not complete this run.\n\n"
                        f"Reason: "
                        f"{type(exc).__name__}\n\n"
                        "Garrick will retry automatically "
                        "in about 10 minutes."
                    )

                except Exception:
                    logger.exception(
                        "Failed to send campaign "
                        "failure notification."
                    )

    finally:
        lock.release()


def _run_due_campaigns():
    now = _utcnow()

    with SessionLocal() as db:
        campaigns = (
            db.query(Campaign)
            .filter(
                Campaign.status == "ACTIVE",
                Campaign.next_run <= now,
            )
            .order_by(
                Campaign.next_run.asc()
            )
            .all()
        )

        ids = [
            campaign.id
            for campaign in campaigns
        ]

    for campaign_id in ids:
        threading.Thread(
            target=_run_campaign,
            args=(campaign_id,),
            name=f"campaign-{campaign_id}",
            daemon=True,
        ).start()


def _loop():
    """
    Campaign scheduler.

    IMPORTANT:
    There is deliberately NO Telegram getUpdates()
    polling here. Telegram commands arrive through
    the FastAPI webhook.
    """

    while True:
        try:
            _run_due_campaigns()

        except Exception:
            logger.exception(
                "Due campaign processing failed"
            )

        time.sleep(
            SCHEDULER_INTERVAL_SECONDS
        )


def start_automation():
    global _AUTOMATION_STARTED

    with _AUTOMATION_LOCK:
        if _AUTOMATION_STARTED:
            logger.info(
                "Garrick automation already started"
            )
            return

        _AUTOMATION_STARTED = True

    # Recover campaigns interrupted by Render restart.
    try:
        _recover_interrupted_campaigns()

    except Exception:
        logger.exception(
            "Interrupted campaign recovery failed"
        )

    # Immediately run campaigns that are already due.
    try:
        _run_due_campaigns()

    except Exception:
        logger.exception(
            "Startup campaign catch-up failed"
        )

    thread = threading.Thread(
        target=_loop,
        name="garrick-automation",
        daemon=True,
    )

    thread.start()

    logger.info(
        "Garrick automation started"
            )
    
