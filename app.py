import os
import json
import re
import time
import html as html_module
import traceback
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import radians, cos, sin, asin, sqrt
from urllib.parse import quote_plus, urlencode

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Geocode cache to avoid rate limits
_geocode_cache = {}


def reverse_geocode_city(lat, lon):
    """Get city and state from lat/lon, with caching and fallback APIs."""
    cache_key = f"{lat:.2f},{lon:.2f}"
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    city, state = None, None

    # Try Nominatim first
    try:
        resp = requests.get(
            f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json",
            headers={"User-Agent": "EventMap/1.0"},
            timeout=6,
        )
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
            addr = resp.json().get("address", {})
            city = addr.get("city", addr.get("town", addr.get("village")))
            state = addr.get("state", "")
    except Exception:
        pass

    # Fallback to BigDataCloud (free, no key, no rate limit)
    if not city:
        try:
            resp = requests.get(
                f"https://api.bigdatacloud.net/data/reverse-geocode-client?latitude={lat}&longitude={lon}&localityLanguage=en",
                timeout=6,
            )
            if resp.status_code == 200:
                data = resp.json()
                city = data.get("city", data.get("locality", ""))
                state = data.get("principalSubdivision", "")
        except Exception:
            pass

    result = (city, state)
    _geocode_cache[cache_key] = result
    return result


def forward_geocode(query):
    """Geocode an address to lat/lon with cache."""
    cache_key = f"fwd:{query[:80]}"
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    result = (None, None)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "EventMap/1.0"},
            timeout=6,
        )
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
            data = resp.json()
            if data:
                result = (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception:
        pass

    _geocode_cache[cache_key] = result
    return result


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Thread-local sessions to avoid connection pool issues in concurrent scraping
_thread_local = threading.local()


def get_session():
    """Get a thread-local requests session."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update(HEADERS)
    return _thread_local.session


def haversine(lat1, lon1, lat2, lon2):
    """Distance in miles between two lat/lon points."""
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 3956 * asin(sqrt(a))


def parse_jsonld_events(soup, source, lat, lon, radius_miles):
    """Parse JSON-LD Event data from a BeautifulSoup page."""
    events = []
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    for script in scripts:
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
            elif isinstance(data, dict) and data.get("@type") == "Event":
                items = [data]
            else:
                continue

            for item in items:
                if isinstance(item, dict) and item.get("@type") != "Event":
                    item = item.get("item", item)
                if not isinstance(item, dict) or item.get("@type") != "Event":
                    continue

                loc = item.get("location", {})
                elat, elon = None, None
                address = ""

                if isinstance(loc, dict):
                    geo = loc.get("geo", {})
                    if isinstance(geo, dict):
                        elat = geo.get("latitude")
                        elon = geo.get("longitude")
                    addr_obj = loc.get("address", {})
                    if isinstance(addr_obj, dict):
                        address = addr_obj.get("streetAddress", "")
                    elif isinstance(addr_obj, str):
                        address = addr_obj

                if elat and elon:
                    try:
                        elat, elon = float(elat), float(elon)
                        if haversine(lat, lon, elat, elon) > radius_miles * 2:
                            continue
                    except (ValueError, TypeError):
                        elat, elon = None, None

                name = item.get("name", "")
                if not name:
                    continue

                loc_name = loc.get("name", "") if isinstance(loc, dict) else str(loc)
                desc = item.get("description", "") or ""
                image = item.get("image", "")
                if isinstance(image, list) and image:
                    image = image[0]

                events.append({
                    "name": name,
                    "url": item.get("url", ""),
                    "date": item.get("startDate", ""),
                    "location": loc_name if loc_name else address,
                    "lat": elat,
                    "lon": elon,
                    "description": desc[:300],
                    "image": image if isinstance(image, str) else "",
                    "source": source,
                })
        except (json.JSONDecodeError, KeyError):
            continue
    return events


# ---------------------------------------------------------------------------
# Eventbrite scraper
# ---------------------------------------------------------------------------
def scrape_eventbrite(lat, lon, radius_miles):
    events = []
    try:
        city, state = reverse_geocode_city(lat, lon)
        if city:
            city_slug = re.sub(r'[^a-z0-9]+', '-', city.lower()).strip('-')
            state_slug = re.sub(r'[^a-z0-9]+', '-', (state or '').lower()).strip('-')
            url = f"https://www.eventbrite.com/d/{state_slug}--{city_slug}/events/"
        else:
            url = f"https://www.eventbrite.com/d/nearby--{lat}%2C{lon}/events/"

        resp = get_session().get(url, timeout=8)
        if resp.status_code != 200:
            return events

        soup = BeautifulSoup(resp.text, "lxml")
        events = parse_jsonld_events(soup, "Eventbrite", lat, lon, radius_miles)

    except Exception:
        traceback.print_exc()
    return events


# ---------------------------------------------------------------------------
# AllEvents API scraper
# ---------------------------------------------------------------------------
def scrape_allevents(lat, lon, radius_miles):
    events = []
    try:
        url = "https://allevents.in/api/index.php/geo/web/explore"
        params = {
            "lat": lat,
            "lng": lon,
            "miles": min(int(radius_miles), 100),
            "category": "",
            "query": "",
        }
        resp = get_session().get(url, params=params, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data", data.get("events", []))
            if isinstance(items, dict):
                items = items.get("events", [])
            for item in items[:50]:
                elat = item.get("lat") or item.get("venue_lat")
                elon = item.get("lng") or item.get("venue_lng") or item.get("lon")
                try:
                    elat, elon = float(elat), float(elon)
                except (ValueError, TypeError):
                    elat, elon = None, None

                if elat and elon and haversine(lat, lon, elat, elon) > radius_miles * 2:
                    continue

                events.append({
                    "name": item.get("eventname", item.get("event_name", "")),
                    "url": item.get("event_url", item.get("url", "")),
                    "date": item.get("start_time", item.get("startDate", "")),
                    "location": item.get("venue_name", item.get("location", "")),
                    "lat": elat,
                    "lon": elon,
                    "description": (item.get("description", "") or "")[:300],
                    "image": item.get("banner_url", item.get("thumb_url", "")),
                    "source": "AllEvents",
                })
    except Exception:
        traceback.print_exc()

    # Fallback: scrape HTML with JSON-LD
    if not events:
        try:
            city, _ = reverse_geocode_city(lat, lon)
            if not city:
                city = "events"
            city_slug = re.sub(r'[^a-z0-9]+', '-', city.lower()).strip('-')
            url = f"https://allevents.in/{city_slug}"
            resp = get_session().get(url, timeout=8)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                events = parse_jsonld_events(soup, "AllEvents", lat, lon, radius_miles)
        except Exception:
            traceback.print_exc()

    return events


# ---------------------------------------------------------------------------
# AllEvents category pages (more events from HTML scraping)
# ---------------------------------------------------------------------------
def scrape_allevents_categories(lat, lon, radius_miles):
    events = []
    try:
        city, _ = reverse_geocode_city(lat, lon)
        if not city:
            return events

        city_slug = re.sub(r'[^a-z0-9]+', '-', city.lower()).strip('-')
        categories = ["/concerts", "/sports"]

        for cat in categories:
            try:
                url = f"https://allevents.in/{city_slug}{cat}"
                resp = get_session().get(url, timeout=10)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "lxml")
                cat_events = parse_jsonld_events(soup, "AllEvents", lat, lon, radius_miles)
                events.extend(cat_events)
            except Exception:
                continue

    except Exception:
        traceback.print_exc()
    return events


# ---------------------------------------------------------------------------
# Meetup scraper
# ---------------------------------------------------------------------------
def scrape_meetup(lat, lon, radius_miles):
    events = []
    try:
        city, state = reverse_geocode_city(lat, lon)

        if city:
            location_str = f"{city}, {state}" if state else city
            url = f"https://www.meetup.com/find/?location={quote_plus(location_str)}&source=EVENTS"
        else:
            url = f"https://www.meetup.com/find/?location={lat}%2C{lon}&source=EVENTS"

        resp = get_session().get(url, timeout=8)
        if resp.status_code != 200:
            return events

        soup = BeautifulSoup(resp.text, "lxml")
        raw_events = parse_jsonld_events(soup, "Meetup", lat, lon, radius_miles)

        # Only keep events with verified coordinates within radius
        for e in raw_events:
            if e.get("lat") and e.get("lon"):
                events.append(e)

    except Exception:
        traceback.print_exc()
    return events


# ---------------------------------------------------------------------------
# Ticketmaster scraper
# ---------------------------------------------------------------------------
def scrape_ticketmaster(lat, lon, radius_miles):
    events = []
    try:
        url = "https://www.ticketmaster.com/discover/events"
        params = {"lat": lat, "long": lon, "radius": int(radius_miles)}
        resp = get_session().get(url, params=params, timeout=8)
        if resp.status_code != 200:
            return events

        soup = BeautifulSoup(resp.text, "lxml")
        events = parse_jsonld_events(soup, "Ticketmaster", lat, lon, radius_miles)

    except Exception:
        traceback.print_exc()
    return events


# ---------------------------------------------------------------------------
# Yelp Events scraper
# ---------------------------------------------------------------------------
def scrape_yelp_events(lat, lon, radius_miles):
    events = []
    try:
        city, state = reverse_geocode_city(lat, lon)
        if not city:
            return events

        city_slug = re.sub(r'[^a-z0-9]+', '+', city.lower()).strip('+')
        state_slug = re.sub(r'[^a-z0-9]+', '+', (state or '').lower()).strip('+')
        location = f"{city_slug}%2C+{state_slug}" if state_slug else city_slug

        url = f"https://www.yelp.com/events/{quote_plus(city)}-{quote_plus(state or '')}"
        resp = get_session().get(url, timeout=8)
        if resp.status_code != 200:
            # Try alternate URL format
            url = f"https://www.yelp.com/search?find_desc=events&find_loc={location}"
            resp = get_session().get(url, timeout=8)
            if resp.status_code != 200:
                return events

        soup = BeautifulSoup(resp.text, "lxml")
        yelp_events = parse_jsonld_events(soup, "Yelp", lat, lon, radius_miles)
        events.extend(yelp_events)

    except Exception:
        traceback.print_exc()
    return events


# ---------------------------------------------------------------------------
# Eventbrite "today/tomorrow" scraper (catches short-notice events)
# ---------------------------------------------------------------------------
def scrape_eventbrite_soon(lat, lon, radius_miles):
    events = []
    try:
        city, state = reverse_geocode_city(lat, lon)
        if not city:
            return events

        city_slug = re.sub(r'[^a-z0-9]+', '-', city.lower()).strip('-')
        state_slug = re.sub(r'[^a-z0-9]+', '-', (state or '').lower()).strip('-')

        # Eventbrite supports date filters: today, tomorrow, this-weekend
        for date_filter in ["tomorrow", "this-weekend"]:
            try:
                url = f"https://www.eventbrite.com/d/{state_slug}--{city_slug}/events--{date_filter}/"
                resp = get_session().get(url, timeout=12)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "lxml")
                page_events = parse_jsonld_events(soup, "Eventbrite", lat, lon, radius_miles)
                events.extend(page_events)
            except Exception:
                continue

    except Exception:
        traceback.print_exc()
    return events


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/events")
def api_events():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = request.args.get("radius", default=10, type=float)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon required"}), 400

    all_events = []

    # Pre-warm reverse geocode cache so scrapers don't each call it
    reverse_geocode_city(lat, lon)

    scrapers = [
        ("eventbrite", scrape_eventbrite),
        ("eventbrite_soon", scrape_eventbrite_soon),
        ("allevents", scrape_allevents),
        ("allevents_cat", scrape_allevents_categories),
        ("meetup", scrape_meetup),
        ("ticketmaster", scrape_ticketmaster),
    ]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fn, lat, lon, radius): name for name, fn in scrapers}
        for future in as_completed(futures, timeout=20):
            source = futures[future]
            try:
                results = future.result(timeout=8)
                print(f"[scraper] {source}: {len(results)} events", flush=True)
                all_events.extend(results)
            except Exception as exc:
                print(f"[scraper] {source} FAILED: {exc}", flush=True)

    # Deduplicate by name similarity
    seen = set()
    unique = []
    for e in all_events:
        key = re.sub(r'\W+', '', e["name"].lower())[:40]
        if key and key not in seen:
            seen.add(key)
            unique.append(e)
        elif not key:
            unique.append(e)

    # Filter out events that geocoded to locations outside the radius
    filtered = []
    for e in unique:
        if e.get("lat") and e.get("lon"):
            try:
                dist = haversine(lat, lon, float(e["lat"]), float(e["lon"]))
                if dist > radius * 2:
                    continue
            except (ValueError, TypeError):
                pass
        filtered.append(e)

    # Drop non-English events (basic heuristic)
    _non_en_words = {'en vivo', 'fiesta de', 'noche de', 'clase de', 'taller de', 'gratis para', 'todos los', 'día de'}
    def _likely_english(name):
        lower = name.lower()
        return not any(phrase in lower for phrase in _non_en_words)
    unique = [e for e in unique if _likely_english(e.get("name", ""))]

    # Drop events with no coords and no location (likely virtual/online)
    # Keep events with coords, or with a location string (placed at pin)
    filtered = [e for e in filtered if (e.get("lat") and e.get("lon")) or e.get("location")]

    # For events still without coords, set them to the pin location
    for e in filtered:
        if not e.get("lat") or not e.get("lon"):
            e["lat"] = lat
            e["lon"] = lon
            e["_no_exact_location"] = True
        # Decode HTML entities in text fields
        for field in ("name", "description", "location"):
            if e.get(field):
                e[field] = html_module.unescape(e[field])

    return jsonify({"events": filtered, "count": len(filtered)})


@app.route("/api/geocode-zip")
def geocode_zip():
    zip_code = request.args.get("zip", "")
    if not re.match(r'^\d{5}$', zip_code):
        return jsonify({"error": "invalid zip"}), 400

    cache_key = f"zip:{zip_code}"
    if cache_key in _geocode_cache:
        lat, lon = _geocode_cache[cache_key]
        if lat and lon:
            return jsonify({"lat": lat, "lon": lon})
        return jsonify({"error": "zip not found"}), 404

    # Try Zippopotam.us (free, no key, no rate limit, US zip codes)
    try:
        resp = get_session().get(f"https://api.zippopotam.us/us/{zip_code}", timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            places = data.get("places", [])
            if places:
                lat = float(places[0]["latitude"])
                lon = float(places[0]["longitude"])
                _geocode_cache[cache_key] = (lat, lon)
                return jsonify({"lat": lat, "lon": lon})
    except Exception:
        pass

    # Fallback: Nominatim
    elat, elon = forward_geocode(f"{zip_code}, United States")
    _geocode_cache[cache_key] = (elat, elon)
    if elat and elon:
        return jsonify({"lat": elat, "lon": elon})

    return jsonify({"error": "zip not found"}), 404


@app.route("/api/reverse-geocode")
def reverse_geocode():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is None or lon is None:
        return jsonify({"error": "lat and lon required"}), 400
    try:
        city, state = reverse_geocode_city(lat, lon)
        return jsonify({"city": city, "state": state})
    except Exception:
        return jsonify({"error": "geocode failed"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050)
