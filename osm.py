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


_LOCATION_CACHE = {}

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

    cached = _LOCATION_CACHE.get(cache_key)

    if cached:
        logger.info(
            "Using cached coordinates for city: %s",
            city,
        )
        return cached

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

        _LOCATION_CACHE[cache_key] = location

        return location

    raise RuntimeError(
        f"Could not resolve location: {city}"
    )


def _build_query(niche, lat, lon):
    """
    Build a manageable Overpass query.

    Uses a 10 km radius instead of 15 km to reduce the
    amount of data returned and lower the chance of
    Overpass gateway timeouts.
    """

    tags = MAP.get(
        niche.lower(),
        [("name", niche)]
    )

    clauses = [
        f'nwr["{key}"="{value}"]'
        f'(around:10000,{lat},{lon});'
        for key, value in tags
    ]

    return (
        "[out:json][timeout:40];"
        "("
        + "".join(clauses)
        + ");"
        "out center tags;"
    )


def _query_overpass(client, query):
    """Try each Overpass server with retries and backoff."""

    last_error = None

    for endpoint in OVERPASS_URLS:

        for attempt in range(2):

            try:
                logger.info(
                    "Trying Overpass endpoint: %s "
                    "(attempt %s/2)",
                    endpoint,
                    attempt + 1,
                )

                response = client.post(
                    endpoint,
                    content=query,
                    headers={
                        "Content-Type": "text/plain",
                        "Accept": "application/json",
                    },
                )

                if response.status_code == 429:
                    last_error = RuntimeError(
                        f"Overpass returned HTTP 429"
                    )

                    logger.warning(
                        "Overpass endpoint rate limited: %s",
                        endpoint,
                    )

                    time.sleep(3)

                    continue

                if response.status_code >= 500:
                    last_error = RuntimeError(
                        f"Overpass returned HTTP "
                        f"{response.status_code}"
                    )

                    logger.warning(
                        "Overpass endpoint failed (%s): "
                        "HTTP %s",
                        endpoint,
                        response.status_code,
                    )

                    time.sleep(2)

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
                    "Overpass endpoint failed (%s): %s",
                    endpoint,
                    exc,
                )

                time.sleep(2)

            except Exception as exc:

                last_error = exc

                logger.warning(
                    "Unexpected Overpass error (%s): %s",
                    endpoint,
                    exc,
                )

                time.sleep(2)

    raise RuntimeError(
        "All Overpass servers failed. "
        "Please try again later."
    ) from last_error


def search_businesses(niche, city, limit):
    """
    Search OpenStreetMap businesses.

    Keeps the existing interface:

        search_businesses(niche, city, limit)

    Returns the existing lead dictionary structure.
    """

    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
    }

    timeout = httpx.Timeout(
        connect=10.0,
        read=50.0,
        write=30.0,
        pool=10.0,
    )

    with httpx.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:

        location = _get_location(
            client,
            city,
        )

        query = _build_query(
            niche,
            location["lat"],
            location["lon"],
        )

        data = _query_overpass(
            client,
            query,
        )

        results = []
        seen = set()

        for element in data.get("elements", []):

            tags_data = element.get(
                "tags",
                {},
            )

            name = tags_data.get("name")

            source_id = (
                f'{element["type"]}/{element["id"]}'
            )

            if not name or source_id in seen:
                continue

            seen.add(source_id)

            center = element.get(
                "center",
                {},
            )

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

                    "address": tags_data.get(
                        "addr:full"
                    ),

                    "city": (
                        tags_data.get("addr:city")
                        or city
                    ),

                    "country": tags_data.get(
                        "addr:country"
                    ),

                    "latitude": element.get(
                        "lat",
                        center.get("lat"),
                    ),

                    "longitude": element.get(
                        "lon",
                        center.get("lon"),
                    ),
                }
            )

            if len(results) >= limit:
                break

        logger.info(
            "OSM search found %s businesses for "
            "%s in %s",
            len(results),
            niche,
            city,
        )

        return results
