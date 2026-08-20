import logging
import os
import re
import time

import httpx


logger = logging.getLogger(__name__)


OVERPASS_URLS = [
    os.getenv(
        "OVERPASS_API_URL",
        "https://overpass-api.de/api/interpreter",
    ),
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


NOMINATIM = os.getenv(
    "NOMINATIM_URL",
    "https://nominatim.openstreetmap.org/search",
)


UA = os.getenv(
    "OSM_USER_AGENT",
    "GarrickAIOutreach/1.0",
)


MAP = {
    "dentist": [
        ("amenity", "dentist"),
    ],
    "dentists": [
        ("amenity", "dentist"),
    ],
    "restaurant": [
        ("amenity", "restaurant"),
    ],
    "restaurants": [
        ("amenity", "restaurant"),
    ],
    "cafe": [
        ("amenity", "cafe"),
    ],
    "cafes": [
        ("amenity", "cafe"),
    ],
    "hotel": [
        ("tourism", "hotel"),
    ],
    "hotels": [
        ("tourism", "hotel"),
    ],
    "pharmacy": [
        ("amenity", "pharmacy"),
    ],
    "pharmacies": [
        ("amenity", "pharmacy"),
    ],
    "gym": [
        ("leisure", "fitness_centre"),
    ],
    "gyms": [
        ("leisure", "fitness_centre"),
    ],
    "beauty salon": [
        ("shop", "beauty"),
    ],
    "beauty salons": [
        ("shop", "beauty"),
    ],
    "hair salon": [
        ("shop", "hairdresser"),
    ],
    "hair salons": [
        ("shop", "hairdresser"),
    ],
}


# City coordinate cache.
_LOCATION_CACHE = {}

# Last public Nominatim request.
_LAST_NOMINATIM_REQUEST = 0.0

# Minimum delay between public Nominatim requests.
NOMINATIM_MIN_INTERVAL = 1.2

# Number of attempts against Nominatim.
NOMINATIM_RETRIES = 3

# Number of attempts against each Overpass endpoint.
OVERPASS_ENDPOINT_RETRIES = 2


def norm(url):
    if not url:
        return None

    return (
        url
        if re.match(
            r"^https?://",
            url,
            re.I,
        )
        else "https://" + url
    )


def _get_location(client, city):
    """
    Resolve a city to coordinates.

    Uses an in-memory cache and throttles public
    Nominatim requests.
    """

    global _LAST_NOMINATIM_REQUEST

    city = (city or "").strip()

    if not city:
        raise RuntimeError(
            "City cannot be empty."
        )

    cache_key = city.lower()

    cached = _LOCATION_CACHE.get(
        cache_key
    )

    if cached:
        logger.info(
            "Using cached coordinates for city: %s",
            city,
        )

        return cached

    for attempt in range(
        1,
        NOMINATIM_RETRIES + 1,
    ):
        elapsed = (
            time.monotonic()
            - _LAST_NOMINATIM_REQUEST
        )

        if elapsed < NOMINATIM_MIN_INTERVAL:
            time.sleep(
                NOMINATIM_MIN_INTERVAL
                - elapsed
            )

        _LAST_NOMINATIM_REQUEST = (
            time.monotonic()
        )

        try:
            response = client.get(
                NOMINATIM,
                params={
                    "q": city,
                    "format": "jsonv2",
                    "limit": 1,
                },
            )

            if response.status_code == 429:
                wait_time = min(
                    10,
                    2 ** attempt,
                )

                logger.warning(
                    "Nominatim rate limited "
                    "request for %s "
                    "(attempt %s/%s). "
                    "Waiting %s seconds.",
                    city,
                    attempt,
                    NOMINATIM_RETRIES,
                    wait_time,
                )

                if attempt < NOMINATIM_RETRIES:
                    time.sleep(
                        wait_time
                    )
                    continue

                raise RuntimeError(
                    "Nominatim rate limit reached "
                    "after multiple attempts."
                )

            response.raise_for_status()

            locations = response.json()

            if not locations:
                raise RuntimeError(
                    f"Could not find the location: {city}"
                )

            location = locations[0]

            _LOCATION_CACHE[
                cache_key
            ] = location

            logger.info(
                "Resolved %s to %s, %s",
                city,
                location["lat"],
                location["lon"],
            )

            return location

        except httpx.HTTPError as exc:
            if attempt >= NOMINATIM_RETRIES:
                raise RuntimeError(
                    "Nominatim location lookup failed "
                    f"for {city}: "
                    f"{type(exc).__name__}"
                ) from exc

            wait_time = min(
                10,
                2 ** attempt,
            )

            logger.warning(
                "Nominatim request failed "
                "for %s: %s. "
                "Retrying in %s seconds.",
                city,
                exc,
                wait_time,
            )

            time.sleep(
                wait_time
            )

    raise RuntimeError(
        f"Could not resolve location: {city}"
    )


def _build_query(
    niche,
    lat,
    lon,
    radius=10000,
):
    """
    Build a bounded Overpass query.

    A smaller radius reduces the chance of 504
    responses on busy Overpass servers.
    """

    tags = MAP.get(
        niche.lower(),
        [("name", niche)],
    )

    clauses = []

    for key, value in tags:
        clauses.append(
            f'nwr["{key}"="{value}"]'
            f"(around:{radius},{lat},{lon});"
        )

    return (
        "[out:json][timeout:20];"
        "("
        + "".join(clauses)
        + ");"
        "out center tags;"
    )


def _query_overpass(
    client,
    query,
):
    """
    Try multiple Overpass servers.

    429, 502, 503 and 504 errors cause the next
    endpoint to be attempted instead of immediately
    killing the campaign.
    """

    last_error = None

    retryable_statuses = {
        429,
        502,
        503,
        504,
    }

    for endpoint in OVERPASS_URLS:

        for attempt in range(
            1,
            OVERPASS_ENDPOINT_RETRIES + 1,
        ):
            try:
                logger.info(
                    "Trying Overpass endpoint: "
                    "%s (attempt %s/%s)",
                    endpoint,
                    attempt,
                    OVERPASS_ENDPOINT_RETRIES,
                )

                response = client.post(
                    endpoint,
                    content=query,
                    headers={
                        "Content-Type":
                            "text/plain",
                        "Accept":
                            "application/json",
                    },
                )

                if (
                    response.status_code
                    in retryable_statuses
                ):
                    last_error = RuntimeError(
                        "Overpass returned HTTP "
                        f"{response.status_code}"
                    )

                    logger.warning(
                        "Overpass endpoint failed "
                        "(%s): HTTP %s.",
                        endpoint,
                        response.status_code,
                    )

                    if attempt < (
                        OVERPASS_ENDPOINT_RETRIES
                    ):
                        time.sleep(
                            2 * attempt
                        )

                        continue

                    break

                response.raise_for_status()

                data = response.json()

                logger.info(
                    "Overpass search succeeded "
                    "using %s",
                    endpoint,
                )

                return data

            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.NetworkError,
                httpx.RequestError,
            ) as exc:

                last_error = exc

                logger.warning(
                    "Overpass endpoint failed "
                    "(%s): %s",
                    endpoint,
                    exc,
                )

                if attempt < (
                    OVERPASS_ENDPOINT_RETRIES
                ):
                    time.sleep(
                        2 * attempt
                    )

            except Exception as exc:

                last_error = exc

                logger.warning(
                    "Unexpected Overpass error "
                    "(%s): %s",
                    endpoint,
                    exc,
                )

                break

    raise RuntimeError(
        "All Overpass servers failed. "
        "Please try again later."
    ) from last_error


def _element_to_lead(
    element,
    niche,
    city,
):
    tags_data = (
        element.get("tags") or {}
    )

    name = tags_data.get("name")

    if not name:
        return None

    element_type = element.get(
        "type",
        "unknown",
    )

    element_id = element.get(
        "id",
        "unknown",
    )

    source_id = (
        f"{element_type}/{element_id}"
    )

    center = (
        element.get("center")
        or {}
    )

    latitude = element.get(
        "lat",
        center.get("lat"),
    )

    longitude = element.get(
        "lon",
        center.get("lon"),
    )

    website = (
        tags_data.get("website")
        or tags_data.get(
            "contact:website"
        )
    )

    phone = (
        tags_data.get("phone")
        or tags_data.get(
            "contact:phone"
        )
    )

    address = (
        tags_data.get("addr:full")
        or " ".join(
            value
            for value in [
                tags_data.get(
                    "addr:housenumber"
                ),
                tags_data.get(
                    "addr:street"
                ),
                tags_data.get(
                    "addr:city"
                ),
            ]
            if value
        )
        or None
    )

    category = (
        tags_data.get("amenity")
        or tags_data.get("shop")
        or tags_data.get("tourism")
        or tags_data.get("leisure")
        or niche
    )

    return {
        "source_id": source_id,
        "name": name,
        "category": category,
        "website": norm(website),
        "phone": phone,
        "address": address,
        "city": (
            tags_data.get("addr:city")
            or city
        ),
        "country": (
            tags_data.get("addr:country")
        ),
        "latitude": latitude,
        "longitude": longitude,
    }


def search_businesses(
    niche,
    city,
    limit,
):
    """
    Search OpenStreetMap businesses.

    Interface remains:

        search_businesses(
            niche,
            city,
            limit
        )

    Businesses WITHOUT websites are intentionally
    returned. They are valid prospects for Garrick.
    """

    if not niche:
        raise ValueError(
            "Niche is required."
        )

    if not city:
        raise ValueError(
            "City is required."
        )

    if limit < 1:
        return []

    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
    }

    timeout = httpx.Timeout(
        connect=10.0,
        read=40.0,
        write=40.0,
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

        lat = location["lat"]
        lon = location["lon"]

        # Start with a smaller radius to reduce Overpass
        # 504/time-out risk.
        query = _build_query(
            niche,
            lat,
            lon,
            radius=10000,
        )

        try:
            data = _query_overpass(
                client,
                query,
            )

        except RuntimeError:
            # One controlled second attempt using a
            # smaller search area. This can help when
            # the original query is too expensive.
            logger.warning(
                "Primary Overpass search failed "
                "for %s in %s. "
                "Trying smaller fallback query.",
                niche,
                city,
            )

            fallback_query = _build_query(
                niche,
                lat,
                lon,
                radius=5000,
            )

            data = _query_overpass(
                client,
                fallback_query,
            )

        results = []
        seen = set()

        for element in (
            data.get("elements") or []
        ):
            source_id = (
                f'{element.get("type")}/'
                f'{element.get("id")}'
            )

            if source_id in seen:
                continue

            lead = _element_to_lead(
                element,
                niche,
                city,
            )

            if not lead:
                continue

            seen.add(source_id)

            results.append(lead)

            if len(results) >= limit:
                break

        logger.info(
            "OSM returned %s businesses "
            "for %s in %s.",
            len(results),
            niche,
            city,
        )

        return results
