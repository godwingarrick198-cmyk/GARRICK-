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


# Cache city coordinates so Garrick does not repeatedly
# ask Nominatim to resolve the same city.
_LOCATION_CACHE = {}

# Track the last Nominatim request so we can respect
# the public Nominatim rate limit.
_LAST_NOMINATIM_REQUEST = 0.0


def norm(u):
    if not u:
        return None

    return (
        u
        if re.match(r"^https?://", u, re.I)
        else "https://" + u
    )


def _get_location(client, city):
    """Resolve city name to latitude/longitude using Nominatim safely."""

    global _LAST_NOMINATIM_REQUEST

    cache_key = city.strip().lower()

    # Use cached coordinates when available.
    cached = _LOCATION_CACHE.get(cache_key)

    if cached:
        logger.info(
            "Using cached coordinates for city: %s",
            city,
        )
        return cached

    # Respect Nominatim's public usage rate.
    elapsed = time.monotonic() - _LAST_NOMINATIM_REQUEST

    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    for attempt in range(3):
        _LAST_NOMINATIM_REQUEST = time.monotonic()

        response = client.get(
            NOMINATIM,
            params={
                "q": city,
                "format": "jsonv2",
                "limit": 1,
            },
        )

        # Nominatim rate limit.
        if response.status_code == 429:
            if attempt < 2:
                wait_time = 2 ** attempt + 1

                logger.warning(
                    "Nominatim rate limited request for %s. "
                    "Retrying in %s seconds.",
                    city,
                    wait_time,
                )

                time.sleep(wait_time)
                continue

            raise RuntimeError(
                "Nominatim rate limit reached. "
                "Please try again later."
            )

        response.raise_for_status()

        locations = response.json()

        if not locations:
            raise RuntimeError(
                f"Could not find the location: {city}"
            )

        location = locations[0]

        # Save the successful result.
        _LOCATION_CACHE[cache_key] = location

        return location

    raise RuntimeError(
        f"Could not resolve location: {city}"
    )


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

            # Try another server for rate limits and server errors.
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

        # Resolve the city. Coordinates are cached after
        # the first successful lookup.
        location = _get_location(client, city)

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
