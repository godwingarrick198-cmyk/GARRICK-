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
from telegram import (
    get_updates,
    notify,
    send_message,
    is_channel_admin,
)

logger = logging.getLogger(__name__)


_FIND_RE = re.compile(
    r"^/find(?:@[A-Za-z0-9_]+)?\s+(.+)$",
    re.I,
)


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
            "Use: /find niche | city | businesses_per_day | days"
        )

    niche, city, per_day_text, days_text = parts

    if not 2 <= len(niche) <= 80:
        raise ValueError(
            "Niche must be between 2 and 80 characters"
        )

    if not 2 <= len(city) <= 120:
        raise ValueError(
            "City must be between 2 and 120 characters"
        )

    try:
        per_day = int(per_day_text)
        days = int(days_text)
    except ValueError as exc:
        raise ValueError(
            "Businesses per day and days must be whole numbers"
        ) from exc

    if not 1 <= per_day <= 50:
        raise ValueError(
            "Businesses per day must be between 1 and 50"
        )

    if not 1 <= days <= 365:
        raise ValueError(
            "Days must be between 1 and 365"
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

    return (
        str(chat_id)
        if chat_id is not None
        else None
    )


def _text_from_update(update):
    message = (
        update.get("message")
        or update.get("channel_post")
    )

    return (message or {}).get("text")


def _handle_find(update):
    text = _text_from_update(update)

    if not text:
        return False

    message = update.get("message") or {}
    chat = message.get("chat") or {}

    # Campaign commands are controlled through private DM.
    if chat.get("type") != "private":
        return True

    try:
        parsed = _parse_find(text)

    except ValueError as exc:
        send_message(
            str(chat.get("id")),
            f"❌ Campaign command error\n\n{exc}",
        )

        return True

    if parsed is None:
        return False

    sender = message.get("from") or {}
    sender_id = sender.get("id")
    private_chat_id = _chat_id_from_update(update)

    if not sender_id or not private_chat_id:
        return True

    # Only an admin/owner of the configured reporting
    # channel can create campaigns.
    if not is_channel_admin(sender_id):
        logger.warning(
            "Ignoring /find from non-admin Telegram user"
        )

        send_message(
            private_chat_id,
            "❌ You must be an admin of Garrick's "
            "reporting channel to create campaigns.",
        )

        return True

    niche, city, per_day, days = parsed

    update_id = update.get("update_id")

    if update_id is None:
        return True

    update_id = int(update_id)
    now = datetime.utcnow()

    with SessionLocal() as db:
        # Prevent the same Telegram command from creating
        # multiple campaigns.
        existing = (
            db.query(Campaign)
            .filter_by(
                telegram_update_id=update_id
            )
            .first()
        )

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
        "Qualified and no-website leads will be "
        "reported to your channel.",
    )

    return True


def _process_lead(db, lead):
    """
    Process one lead.

    Businesses without websites are intentionally NOT sent
    through website analysis or Gemini. Instead, they are
    marked NO_WEBSITE and reported directly to Telegram.
    """

    # ---------------------------------------------------------
    # NO WEBSITE
    # ---------------------------------------------------------
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
                "💡 Opportunity: This business may be "
                "a potential website prospect."
            )

        except Exception:
            logger.exception(
                "Telegram notification failed for "
                "no-website lead %s",
                lead.id,
            )

        return

    # ---------------------------------------------------------
    # ALREADY PROCESSED
    # ---------------------------------------------------------
    if lead.status in (
        LeadStatus.ANALYZED,
        LeadStatus.QUALIFIED,
        LeadStatus.CONTACTED,
        LeadStatus.CLIENT,
    ):
        return

    # ---------------------------------------------------------
    # WEBSITE ANALYSIS + GEMINI
    # ---------------------------------------------------------
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
            []
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

    # ---------------------------------------------------------
    # QUALIFIED LEAD TELEGRAM REPORT
    # ---------------------------------------------------------
    if lead.status == LeadStatus.QUALIFIED:
        try:
            notify(
                "🔥 QUALIFIED LEAD\n\n"
                f"Business: {lead.name}\n"
                f"City: {lead.city}\n"
                f"Score: {lead.lead_score}/100\n"
                f"Website: {lead.website}\n"
                f"Opportunity: {lead.opportunity}\n"
                f"Problems: "
                f"{', '.join(lead.problems or [])}"
            )

        except Exception:
            logger.exception(
                "Telegram notification failed for "
                "qualified lead %s",
                lead.id,
            )


def _run_campaign(campaign_id):
    with SessionLocal() as db:
        campaign = db.get(
            Campaign,
            campaign_id,
        )

        if (
            not campaign
            or campaign.status != "ACTIVE"
        ):
            return

        now = datetime.utcnow()

        if campaign.next_run > now:
            return

        logger.info(
            "Running campaign %s: day %s/%s",
            campaign.id,
            campaign.days_completed + 1,
            campaign.total_days,
        )

        try:
            # Search a larger pool so duplicate/existing
            # businesses do not consume the daily quota.
            pool_size = min(
                max(
                    campaign.leads_per_day * 5,
                    campaign.leads_per_day,
                ),
                50,
            )

            rows = search_businesses(
                campaign.niche,
                campaign.city,
                pool_size,
            )

            selected = []

            for row in rows:
                source_id = row.get(
                    "source_id"
                )

                if not source_id:
                    continue

                # Existing OSM businesses are skipped.
                # This prevents the same business from being
                # processed repeatedly across campaign days.
                existing = (
                    db.query(Lead)
                    .filter_by(
                        source_id=source_id
                    )
                    .first()
                )

                if existing:
                    continue

                lead = Lead(**row)

                db.add(lead)
                db.commit()
                db.refresh(lead)

                selected.append(lead)

                if (
                    len(selected)
                    >= campaign.leads_per_day
                ):
                    break

            # Process all newly selected businesses.
            for lead in selected:
                _process_lead(
                    db,
                    lead,
                )

            # Mark this campaign day complete only after
            # all selected leads have been processed.
            completed_at = datetime.utcnow()

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
                campaign.next_run = (
                    campaign.next_run
                    + timedelta(days=1)
                )

            db.commit()

            # Campaign progress notification.
            # Telegram failure must not undo the database
            # transaction.
            try:
                notify(
                    "📊 Campaign update\n\n"
                    f"Campaign {campaign.id}: "
                    f"{campaign.niche} in "
                    f"{campaign.city}\n"
                    f"Completed day "
                    f"{campaign.days_completed}/"
                    f"{campaign.total_days}\n"
                    f"New businesses processed: "
                    f"{len(selected)}"
                )

            except Exception:
                logger.exception(
                    "Campaign notification failed "
                    "for campaign %s",
                    campaign.id,
                )

        except Exception as exc:
            db.rollback()

            # Re-fetch the campaign after rollback.
            campaign = db.get(
                Campaign,
                campaign_id,
            )

            if (
                campaign
                and campaign.status == "ACTIVE"
            ):
                campaign.last_error = (
                    type(exc).__name__
                )

                # Retry failed campaign work later
                # instead of losing the campaign.
                campaign.next_run = (
                    datetime.utcnow()
                    + timedelta(hours=1)
                )

                db.commit()

            logger.exception(
                "Campaign %s failed; retrying later",
                campaign_id,
            )


def _run_due_campaigns():
    now = datetime.utcnow()

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
        _run_campaign(
            campaign_id
        )


def _loop():
    offset = None

    while True:
        try:
            updates = get_updates(
                offset=offset,
                timeout=20,
            )

            for update in updates:
                offset = (
                    int(update["update_id"])
                    + 1
                )

                try:
                    _handle_find(update)

                except Exception:
                    logger.exception(
                        "Telegram command "
                        "processing failed"
                    )

        except Exception:
            logger.exception(
                "Telegram polling cycle failed"
            )

            time.sleep(10)

        try:
            _run_due_campaigns()

        except Exception:
            logger.exception(
                "Due campaign processing failed"
            )


def start_automation():
    thread = threading.Thread(
        target=_loop,
        name="garrick-automation",
        daemon=True,
    )

    thread.start()

    logger.info(
        "Garrick automation started"
    )
