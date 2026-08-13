import os
import re
import logging
import time
import httpx

OVERPASS_URLS = [
    os.getenv(
        "OVERPASS_API_URL",
        "https://overpass-api.de/api/interpreter"
    ),
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

NOMINATIM = os.getenv(
    "NOMINATIM_URL",
    "https://nominatim.openstreetmap.org/search"
)

UA = os.getenv(
    "OSM_USER_AGENT",
    "GarrickAIOutreach/1.0"
)

MAP = {
    "dentist": [("amenity", "dentist")],
    "dentists": [("amenity", "dentist")],
    "restaurant": [("amenity", "restaurant")],
    "restaurants": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "hotel": [("tourism", "hotel")],
    "pharmacy": [("amenity", "pharmacy")],
    "gym": [("leisure", "fitness_centre")],
    "beauty salon": [("shop", "beauty")],
    "hair salon": [("shop", "hairdresser")],
}

logger = logging.getLogger(__name__)


def norm(u):
    if not u:
        return None

    return (
        u
        if re.match(r"^https?://", u, re.I)
        else "https://" + u
    )


def _get_location(client, city):
    """Resolve city name to latitude/longitude using Nominatim."""

    response = client.get(
        NOMINATIM,
        params={
            "q": city,
            "format": "jsonv2",
            "limit": 1,
        },
    )

    response.raise_for_status()

    locations = response.json()

    if not locations:
        raise RuntimeError(
            f"Could not find the location: {city}"
        )

    return locations[0]


def _build_query(niche, lat, lon):
    """Build a relatively small Overpass query."""

    tags = MAP.get(
        niche.lower(),
        [("name", niche)]
    )

    clauses = [
        f'nwr["{key}"="{value}"]'
        f'(around:15000,{lat},{lon});'
        for key, value in tags
    ]

    return (
        "[out:json][timeout:25];"
        "("
        + "".join(clauses)
        + ");"
        "out center tags;"
    )


def _query_overpass(client, query):
    """Try each Overpass server until one succeeds."""

    last_error = None

    for endpoint in OVERPASS_URLS:
        try:
            logger.info(
                "Trying Overpass endpoint: %s",
                endpoint
            )

            response = client.post(
                endpoint,
                content=query,
                headers={
                    "Content-Type": "text/plain",
                    "Accept": "application/json",
                },
            )

            # Retry another server for rate limits and server errors.
            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(
                    f"Overpass returned HTTP {response.status_code}"
                )

                logger.warning(
                    "Overpass endpoint failed (%s): HTTP %s. "
                    "Trying next endpoint.",
                    endpoint,
                    response.status_code,
                )

                continue

            response.raise_for_status()

            return response.json()

        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.RequestError,
        ) as exc:
            last_error = exc

            logger.warning(
                "Overpass endpoint failed (%s): %s. "
                "Trying next endpoint.",
                endpoint,
                exc,
            )

        except Exception as exc:
            last_error = exc

            logger.warning(
                "Unexpected Overpass error (%s): %s. "
                "Trying next endpoint.",
                endpoint,
                exc,
            )

    raise RuntimeError(
        "All Overpass servers failed. "
        "Please try again later."
    ) from last_error


def search_businesses(niche, city, limit):
    """
    Search OpenStreetMap businesses.

    Keeps the existing interface:
        search_businesses(niche, city, limit)

    Returns the same lead dictionary structure as the
    previous implementation.
    """

    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
    }

    # 30 seconds for each HTTP request.
    timeout = httpx.Timeout(
        connect=10.0,
        read=30.0,
        write=30.0,
        pool=10.0,
    )

    with httpx.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:

        # Resolve the city first.
        location = _get_location(client, city)

        # Respect Nominatim usage policy by avoiding
        # an immediate second request.
        time.sleep(1)

        tags = MAP.get(
            niche.lower(),
            [("name", niche)]
        )

        clauses = [
            f'nwr["{key}"="{value}"]'
            f'(around:15000,{location["lat"]},{location["lon"]});'
            for key, value in tags
        ]

        query = (
            "[out:json][timeout:25];"
            "("
            + "".join(clauses)
            + ");"
            "out center tags;"
        )

        data = _query_overpass(
            client,
            query
        )

        results = []
        seen = set()

        for element in data.get("elements", []):
            tags_data = element.get("tags", {})

            name = tags_data.get("name")

            source_id = (
                f'{element["type"]}/{element["id"]}'
            )

            # Avoid duplicates.
            if not name or source_id in seen:
                continue

            seen.add(source_id)

            center = element.get("center", {})

            results.append(
                {
                    "source_id": source_id,
                    "name": name,
                    "category": (
                        tags_data.get("amenity")
                        or tags_data.get("shop")
                        or tags_data.get("tourism")
                        or niche
                    ),
                    "website": norm(
                        tags_data.get("website")
                        or tags_data.get("contact:website")
                    ),
                    "phone": (
                        tags_data.get("phone")
                        or tags_data.get("contact:phone")
                    ),
                    "address": tags_data.get("addr:full"),
                    "city": (
                        tags_data.get("addr:city")
                        or city
                    ),
                    "country": tags_data.get("addr:country"),
                    "latitude": element.get(
                        "lat",
                        center.get("lat")
                    ),
                    "longitude": element.get(
                        "lon",
                        center.get("lon")
                    ),
                }
            )

            if len(results) >= limit:
                break

        return results
