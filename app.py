"""
Minnesota Child Care Finder — Flask API server

Serves the static front-end and a set of JSON endpoints used by the search UI.
All provider data is read from the SQLite database written by importer.py.

Endpoints:
  GET /                   → docs/index.html
  GET /api/geocode        → resolve a city/ZIP string to lat/lng
  GET /api/locations      → distinct city+ZIP pairs with provider counts
  GET /api/ai-search      → natural-language search with scoring

Environment variables:
  FLASK_HOST   bind address (default 0.0.0.0)
  FLASK_PORT   port (default 5000)
  FLASK_DEBUG  set to 1/true/yes to enable Flask debug mode
"""
from flask import Flask, jsonify, request, send_from_directory
import os, sqlite3, math, re, urllib.request, urllib.parse, json
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB   = BASE / 'data' / 'childcare.db'
app  = Flask(__name__, static_folder='docs', static_url_path='')

print(f'[startup] BASE={BASE}')
print(f'[startup] DB={DB} exists={DB.exists()}')

# Canonical day order — matches the column order in the providers table.
DAYS = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']

# Fallback centre-of-Minnesota coords used when no location can be resolved.
MN_DEFAULT_LOCATION = ('Minnesota', 46.2807, -94.3053)

# Hard radius cap for location-anchored searches.
MAX_SEARCH_MILES = 30

# Hard-coded centroids for common Minnesota cities so we can avoid a Nominatim
# round-trip for the most frequent searches.
MN_CITY_COORDS = {
    'minneapolis': (44.9778, -93.2650), 'st paul': (44.9537, -93.0900),
    'saint paul': (44.9537, -93.0900), 'duluth': (46.7867, -92.1005),
    'rochester': (44.0121, -92.4802), 'bloomington': (44.8408, -93.2983),
    'brooklyn park': (45.0941, -93.3563), 'plymouth': (45.0105, -93.4555),
    'woodbury': (44.9239, -92.9594), 'maple grove': (45.0725, -93.4558),
    'eden prairie': (44.8547, -93.4708), 'burnsville': (44.7677, -93.2777),
    'st cloud': (45.5579, -94.1632), 'saint cloud': (45.5579, -94.1632),
    'mankato': (44.1636, -93.9994), 'brainerd': (46.3580, -94.2008),
    'moorhead': (46.8739, -96.7678), 'waite park': (45.5572, -94.2251),
    'marshall': (44.4489, -95.7883), 'willmar': (45.1219, -95.0433),
    'dawson': (44.9308, -96.0536),
}

# Simple in-process cache so repeated geocode calls for the same string are free.
GEOCODE_CACHE = {}

# Maps every common day abbreviation/alias to the canonical lowercase day name.
DAY_ALIASES = {
    'mon':'monday','monday':'monday','tue':'tuesday','tues':'tuesday','tuesday':'tuesday',
    'wed':'wednesday','wednesday':'wednesday','thu':'thursday','thur':'thursday',
    'thurs':'thursday','thursday':'thursday','fri':'friday','friday':'friday',
    'sat':'saturday','saturday':'saturday','sun':'sunday','sunday':'sunday'
}

# AI_INTENTS: phrases that a user might type to express a care need or age group.
# Used in rule_parse_query to detect what the user is asking for.
AI_INTENTS = {
    'special needs': ['special needs','special-needs','disability','disabled','extra support','inclusion','accommodation'],
    'autism':    ['autism','autistic','asd'],
    'behavior':  ['behavior','behaviour','behavioral','emotional support','mental health'],
    'medical':   ['medical','medication','nurse','allergy','allergies','feeding support','health plan'],
    'sensory':   ['sensory','occupational therapy','ot therapy'],
    'language':  ['language','bilingual','multilingual','spanish','somali','hmong'],
    'infant':    ['infant','baby','babies','newborn','under 1','0-12 months'],
    'toddler':   ['toddler','toddlers','1-2 years'],
    'preschool': ['preschool','pre-k','pre k','prekindergarten','pre-kindergarten'],
}

# INTENT_TERMS: substrings searched inside provider features/raw_search fields to
# confirm that the provider actually mentions the capability.  Deliberately more
# conservative than AI_INTENTS to avoid false positives.
INTENT_TERMS = {
    'special needs': ['special need','disabilit','accommodation','inclusion','inclusive','individualized','iep','ifsp','developmental','sensory'],
    'autism':    ['autism','autistic','asd'],
    'behavior':  ['behavior','behaviour','behavioral','mental health'],
    'medical':   ['medical','medication','nurse','allerg','feeding','health plan'],
    'sensory':   ['sensory','occupational therapy'],
    'language':  ['bilingual','multilingual','spanish','somali','hmong'],
    'infant':    ['infant','baby','0-12','6 weeks'],
    'toddler':   ['toddler','1-2 years'],
    'preschool': ['preschool','pre-k','prekindergarten'],
}

# Columns returned by all provider-list endpoints.  Kept as a string constant so
# it is easy to extend — add a column here and it will appear in all responses.
SELECT_COLS = 'id,name,type,rating,license,address,city,state,zip,county,prog_type,phone,email,website,lat,lng,monday,tuesday,wednesday,thursday,friday,saturday,sunday,hours_source,features,ages_served,age_range,age_openings'


# ── Database helpers ──────────────────────────────────────────────────────────

def get_db():
    """Open and return a SQLite connection with Row factory enabled."""
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def ensure_provider_columns():
    """
    Migrate older databases that predate the age_range / age_openings columns.
    Called once at startup so the app works against any DB version.
    """
    if not DB.exists():
        return
    with sqlite3.connect(DB) as c:
        existing = {row[1] for row in c.execute('PRAGMA table_info(providers)')}
        for column in ('age_range', 'age_openings'):
            if column not in existing:
                c.execute(f'ALTER TABLE providers ADD COLUMN {column} TEXT')

ensure_provider_columns()


# ── Math / scoring helpers ────────────────────────────────────────────────────

def distance_miles(lat1, lng1, lat2, lng2):
    """Haversine great-circle distance in miles between two lat/lng points."""
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    # Clamp to [0,1] before asin to guard against floating-point rounding errors.
    return 2 * R * math.asin(math.sqrt(max(0, min(1, h))))

def rating_score(v):
    """
    Convert a rating string (e.g. "3 Star", "4.5") to a 0–1 score.
    Assumes a 4-point scale; returns 0.5 when no number is found.
    """
    m = re.search(r'(\d+(?:\.\d+)?)', v or '')
    return min(float(m.group(1)) / 4, 1) if m else 0.5

def parse_time(text):
    """
    Extract the first time expression from text.
    Handles 12-hour (7am, 6:30 PM) and 24-hour (14:30) formats.
    Returns (minutes_since_midnight: int | None, matched_text: str).
    """
    # 12-hour format with am/pm
    m = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b', text, re.I)
    if m:
        h, mins, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower().replace('.','')
        if h == 12: h = 0       # 12:xx am/pm → treat 12 as 0 before offset
        if mer == 'pm': h += 12
        return h * 60 + mins, m.group(0)
    # 24-hour format (HH:MM)
    m = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', text)
    if m: return int(m.group(1)) * 60 + int(m.group(2)), m.group(0)
    return None, ''

def time_in_hours(value, target):
    """
    Return True if target (minutes since midnight) falls within any open–close
    range found in the hours string `value`.
    e.g. value="7:00 AM - 6:00 PM", target=480 (8am) → True
    """
    if target is None: return False
    for s, e in re.findall(
        r'(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\s*[-–]\s*(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)',
        value or '', re.I
    ):
        sm, _ = parse_time(s)
        em, _ = parse_time(e)
        if sm is not None and em is not None and sm <= target <= em:
            return True
    return False

def hour_bounds(value):
    """
    Return all (open_minutes, close_minutes) pairs found in a hours string.
    Used to compute opening time, closing time, and total open minutes for sorting.
    """
    bounds = []
    for start, end in re.findall(
        r'(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\s*[-–]\s*(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)',
        value or '', re.I
    ):
        start_minutes, _ = parse_time(start)
        end_minutes, _ = parse_time(end)
        if start_minutes is not None and end_minutes is not None:
            bounds.append((start_minutes, end_minutes))
    return bounds

def hour_sort_mode(text):
    """
    Detect whether the user wants results sorted by a specific hours criterion.
    Returns one of 'opens_earliest', 'closes_latest', 'longest', or '' (default).
    """
    lower = text.lower()
    if re.search(r'\b(earliest|opens first|open earliest|earliest opening)\b', lower):
        return 'opens_earliest'
    if re.search(r'\b(latest|opens latest|close latest|latest closing|open longest)\b', lower):
        return 'closes_latest'
    if re.search(r'\b(longest hours|most hours|longest day)\b', lower):
        return 'longest'
    return ''

def score_intents(features, raw, need_labels):
    """
    Return the subset of need_labels that are actually mentioned in the provider's
    features or raw_search text.  Uses INTENT_TERMS for substring matching.
    Only labels with a confirmed text match are returned — nothing is inferred.
    """
    text = ((features or '') + ' ' + (raw or '')).lower()
    return [l for l in need_labels if any(w in text for w in INTENT_TERMS.get(l, []))]


# ── Geocoding helpers ─────────────────────────────────────────────────────────

def geocode(query_str):
    """
    Resolve a free-text address to (lat, lng) using the Nominatim OSM geocoder.
    Returns (None, None) on failure or empty results.
    Used as a last resort when the local DB lookup fails.
    """
    print(f'[geocode] querying Nominatim for: {query_str!r}')
    try:
        q = urllib.parse.urlencode({'q': query_str, 'format': 'json', 'limit': '1', 'countrycodes': 'us'})
        req = urllib.request.Request(
            'https://nominatim.openstreetmap.org/search?' + q,
            headers={'User-Agent': 'LittleBloom/1.0'}
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        if data:
            lat, lon = float(data[0]['lat']), float(data[0]['lon'])
            print(f'[geocode] result: lat={lat}, lng={lon}')
            return lat, lon
        print(f'[geocode] no results returned')
    except Exception as e:
        print(f'[geocode] ERROR: {e}')
    return None, None

def geocode_from_db(city_word):
    """
    Find a city centroid by averaging the coordinates of all providers in that city.
    Tries an exact match first, then a prefix LIKE match.
    No network call — uses only the local database.
    Returns (lat, lng) or (None, None).
    """
    print(f'[geocode_from_db] looking up city_word={city_word!r}')
    try:
        with get_db() as c:
            rows = c.execute(
                "SELECT lat, lng FROM providers WHERE LOWER(city)=? AND lat IS NOT NULL LIMIT 30",
                (city_word.lower(),)
            ).fetchall()
            print(f'[geocode_from_db] exact match rows={len(rows)}')
            if not rows:
                rows = c.execute(
                    "SELECT lat, lng FROM providers WHERE LOWER(city) LIKE ? AND lat IS NOT NULL LIMIT 30",
                    (city_word.lower() + '%',)
                ).fetchall()
                print(f'[geocode_from_db] LIKE match rows={len(rows)}')
        if rows:
            lat = sum(r['lat'] for r in rows)/len(rows)
            lng = sum(r['lng'] for r in rows)/len(rows)
            print(f'[geocode_from_db] centroid: lat={lat:.4f}, lng={lng:.4f}')
            return lat, lng
        print(f'[geocode_from_db] no rows found for {city_word!r}')
    except Exception as e:
        print(f'[geocode_from_db] ERROR: {e}')
    return None, None

def geocode_zip_from_db(zip_code):
    """
    Calculate a ZIP code centroid from provider coordinates in the local database.
    Matches the full 5-digit code or any zip that starts with those digits.
    Returns (lat, lng) or (None, None).
    """
    try:
        with get_db() as c:
            rows = c.execute(
                "SELECT lat, lng FROM providers WHERE (zip=? OR zip LIKE ?) AND lat IS NOT NULL AND lng IS NOT NULL LIMIT 100",
                (zip_code, zip_code + '%')
            ).fetchall()
        if rows:
            return sum(r['lat'] for r in rows) / len(rows), sum(r['lng'] for r in rows) / len(rows)
    except Exception as e:
        print(f'[geocode_zip_from_db] ERROR: {e}')
    return None, None

def resolve_location_text(value):
    """
    Resolve a user-typed location string (city name or ZIP) to (lat, lng, label).

    Resolution order:
      1. Return from GEOCODE_CACHE if already resolved.
      2. ZIP code → geocode_zip_from_db → Nominatim fallback.
      3. Known MN city in MN_CITY_COORDS → return immediately.
      4. geocode() via Nominatim.

    Returns (lat, lng, label) where label is the original string, or
    (None, None, None) if resolution fails entirely.
    """
    value = re.sub(r'\s+', ' ', (value or '').strip())
    if not value:
        return None, None, None
    cache_key = value.lower()
    if cache_key in GEOCODE_CACHE:
        return GEOCODE_CACHE[cache_key]

    zip_match = re.fullmatch(r'\d{5}(?:-\d{4})?', value)
    if zip_match:
        zip_code = value[:5]
        lat, lng = geocode_zip_from_db(zip_code)
        query = f'{zip_code}, Minnesota, USA'
    else:
        key = re.sub(r'[^a-z ]', '', value.lower()).strip()
        if key in MN_CITY_COORDS:
            lat, lng = MN_CITY_COORDS[key]
            result = (lat, lng, value)
            GEOCODE_CACHE[cache_key] = result
            return result
        lat = lng = None
        query = f'{value}, Minnesota, USA'

    if lat is None or lng is None:
        lat, lng = geocode(query)

    result = (lat, lng, value if lat is not None else None)
    GEOCODE_CACHE[cache_key] = result
    return result


# ── Query parser ──────────────────────────────────────────────────────────────

def rule_parse_query(text):
    """
    Deterministic, local query parser.
    Extracts: ZIP, day, age group, special needs, open time, hours sort, city.

    City resolution is tried in four passes (see inline comments).
    Returns a dict with keys: city, zip, day, age_group, needs, open_time, hours_sort.
    """
    print(f'[rule_parse_query] called with text={text!r}')
    lower = text.lower()

    # 1. ZIP
    zm = re.search(r'\b(\d{5})\b', text)
    zip_code = zm.group(1) if zm else None
    print(f'[rule_parse_query] zip={zip_code!r}')

    # 2. Day
    day = ''
    for k, v in DAY_ALIASES.items():
        if re.search(r'\b' + re.escape(k) + r'\b', lower):
            day = v; break
    print(f'[rule_parse_query] day={day!r}')

    # 3. Age group
    age_group = None
    for label, phrases in AI_INTENTS.items():
        if label in ('infant','toddler','preschool'):
            if any(p in lower for p in phrases):
                age_group = label; break
    print(f'[rule_parse_query] age_group={age_group!r}')

    # 4. Special needs / language / other intent labels
    needs = [label for label, phrases in AI_INTENTS.items()
             if label not in ('infant','toddler','preschool')
             and any(p in lower for p in phrases)]
    print(f'[rule_parse_query] needs={needs}')

    # 5. Open time and hours sort preference
    _, open_time = parse_time(lower)
    print(f'[rule_parse_query] open_time={open_time!r}')
    hours_sort = hour_sort_mode(lower)

    # 6. City — pass 1: explicit "near/in/around X" phrase with preposition anchor.
    #    Supports multi-word Minnesota town names.
    city = None
    city_match = re.search(
        r'\b(?:near|in|around|from|by|closest to)\s+([a-z][a-z .\'-]+?)(?=\s+(?:open|that|with|for|on|at|within|offering)\b|\s*$)',
        lower,
    )
    print(f'[rule_parse_query] city_match regex matched={city_match is not None}')
    if city_match:
        candidate = re.sub(r'\s+', ' ', city_match.group(1)).strip(' .,-')
        # "st X" is ambiguous — also try "saint X" in case the DB uses the full spelling.
        city_candidates = [candidate]
        if candidate.startswith('st '):
            city_candidates.append('saint ' + candidate[3:])
        print(f'[rule_parse_query] city_candidates from regex={city_candidates}')
        with get_db() as c:
            row = c.execute(
                "SELECT city FROM providers WHERE LOWER(city)=? OR LOWER(city) LIKE ? LIMIT 1",
                (city_candidates[-1], city_candidates[-1] + '%'),
            ).fetchone()
            if not row and len(city_candidates) > 1:
                row = c.execute(
                    "SELECT city FROM providers WHERE LOWER(city)=? OR LOWER(city) LIKE ? LIMIT 1",
                    (city_candidates[0], city_candidates[0] + '%'),
                ).fetchone()
        # Keep the phrase even when no local provider has that town yet;
        # resolve_coordinates can locate any Minnesota town through Nominatim.
        city = row['city'] if row else candidate
        print(f'[rule_parse_query] city from regex phrase match={city!r}')

    # 7. City — pass 2: scan remaining words against the full list of known provider cities.
    #    Long city names are tested first to prevent partial matches (e.g. "Paul" before "St Paul").
    stop = {'mon','tue','wed','thu','fri','sat','sun','monday','tuesday','wednesday',
            'thursday','friday','saturday','sunday','infant','toddler','preschool',
            'baby','open','care','near','find','show','want','need','with','the',
            'and','for','has','who','can','any','some','please','looking','home',
            'program','center','daycare','childcare','family','special','needs',
            'autism','medical','sensory','language','bilingual','help','good','best',
            'rated','rating','ages','age','kids','children','child','mn','minnesota',
            'bilingual','spanish','somali','hmong','behavior','sensory','medical'}
    words = [w for w in re.findall(r'[a-z]{3,}', lower) if w not in stop]
    print(f'[rule_parse_query] words after stoplist={words}')

    if city is None:
        with get_db() as c:
            # Prefer the city column; fall back to cities parsed from raw_search when city is empty.
            known_cities = [row['city'] for row in c.execute(
                "SELECT DISTINCT city FROM providers WHERE city<>'' ORDER BY LENGTH(city) DESC"
            ).fetchall()]
            print(f'[rule_parse_query] known_cities from city column: {len(known_cities)} entries, sample={known_cities[:5]}')
            if not known_cities:
                # Older databases (ArcGIS source) may not populate the city column;
                # fall back to parsing "..., MN ..." fragments out of raw_search.
                print('[rule_parse_query] city column is empty — falling back to raw_search parsing')
                raw_cities = c.execute(
                    "SELECT DISTINCT TRIM(SUBSTR(raw_search, INSTR(raw_search, ', MN') - 40, 40)) as chunk "
                    "FROM providers WHERE raw_search LIKE '%, MN %' LIMIT 500"
                ).fetchall()
                print(f'[rule_parse_query] raw_search chunks fetched: {len(raw_cities)}')
                for row in raw_cities:
                    m = re.search(r'([A-Z][a-zA-Z .\']+),\s*MN', row['chunk'] or '')
                    if m:
                        candidate_city = m.group(1).strip()
                        if candidate_city not in known_cities:
                            known_cities.append(candidate_city)
                known_cities.sort(key=len, reverse=True)
                print(f'[rule_parse_query] cities from raw_search: {len(known_cities)} entries, sample={known_cities[:5]}')
        normalized_query = re.sub(r'[^a-z0-9 ]', ' ', lower)
        normalized_query = re.sub(r'\s+', ' ', normalized_query)
        print(f'[rule_parse_query] normalized_query={normalized_query!r}')
        for known_city in known_cities:
            normalized_city = re.sub(r'[^a-z0-9 ]', ' ', known_city.lower())
            normalized_city = re.sub(r'\s+', ' ', normalized_city).strip()
            if re.search(r'\b' + re.escape(normalized_city) + r'\b', normalized_query):
                city = known_city
                print(f'[rule_parse_query] matched city from known_cities list: {city!r}')
                break
        if city is None:
            print('[rule_parse_query] no city matched from known_cities list')

    # 8. City — pass 3: try each remaining word against geocode_from_db.
    if city is None:
        print(f'[rule_parse_query] trying geocode_from_db fallback for words={words}')
        for w in words:
            lat, lng = geocode_from_db(w)
            if lat is not None:
                with get_db() as c:
                    row = c.execute("SELECT city FROM providers WHERE LOWER(city) LIKE ? LIMIT 1", (w+'%',)).fetchone()
                city = row['city'] if row else w.title()
                print(f'[rule_parse_query] city from geocode_from_db word={w!r} → city={city!r}')
                break

    # 9. City — pass 4: very short queries (≤3 non-stopword tokens) that didn't
    #    match any known city are treated as a bare town name and passed to Nominatim.
    if city is None and not zip_code:
        simple_words = re.findall(r"[a-z][a-z.'-]*", lower)
        if len(simple_words) <= 3 and not any(word in stop for word in simple_words):
            city = re.sub(r'\s+', ' ', lower).strip(' .,-').title()
            print(f'[rule_parse_query] town-only fallback city={city!r}')

    print(f'[rule_parse_query] FINAL city={city!r}')
    return {
        'city': city,
        'zip': zip_code,
        'day': day or None,
        'age_group': age_group,
        'needs': needs,
        'open_time': open_time or None,
        'hours_sort': hours_sort or None
    }


# ── Location resolver ─────────────────────────────────────────────────────────

def resolve_coordinates(parsed, caller_lat, caller_lng):
    """
    Determine the (lat, lng) origin to use for distance ranking.

    Priority:
      1. Caller-supplied GPS coords (from browser geolocation).
      2. ZIP code from parsed intent → provider DB centroid → Nominatim.
      3. City from parsed intent → MN_CITY_COORDS → DB centroid → Nominatim.
      4. Falls back to the centre of Minnesota when nothing resolves.

    Returns (lat, lng).  Never returns None; always has a fallback.
    """
    print(f'[resolve_coordinates] parsed_city={parsed.get("city")!r} parsed_zip={parsed.get("zip")!r} caller_lat={caller_lat} caller_lng={caller_lng}')
    if caller_lat is not None and caller_lng is not None:
        print(f'[resolve_coordinates] using caller coords: {caller_lat}, {caller_lng}')
        return caller_lat, caller_lng

    # ZIP takes priority over city when both are present.
    if parsed.get('zip'):
        print(f'[resolve_coordinates] trying provider ZIP centroid: {parsed["zip"]}')
        lat, lng = geocode_zip_from_db(parsed['zip'])
        if lat:
            print(f'[resolve_coordinates] provider ZIP centroid succeeded: {lat}, {lng}')
            return lat, lng
        print('[resolve_coordinates] provider ZIP centroid unavailable; trying Nominatim')
        lat, lng = geocode(parsed['zip'] + ', Minnesota, USA')
        if lat:
            return lat, lng

    if parsed.get('city'):
        city_key = re.sub(r'[^a-z ]', '', parsed['city'].lower()).strip()
        if city_key in MN_CITY_COORDS:
            # Fast path — no DB or network call needed.
            return MN_CITY_COORDS[city_key]
        print(f'[resolve_coordinates] trying DB centroid for city={parsed["city"]!r}')
        lat, lng = geocode_from_db(parsed['city'])
        if lat:
            print(f'[resolve_coordinates] DB centroid succeeded: {lat}, {lng}')
            return lat, lng
        print(f'[resolve_coordinates] DB centroid failed, trying Nominatim for city={parsed["city"]!r}')
        lat, lng = geocode(parsed['city'] + ', Minnesota, USA')
        if lat:
            print(f'[resolve_coordinates] Nominatim city geocode succeeded: {lat}, {lng}')
            return lat, lng
        print('[resolve_coordinates] Nominatim city geocode also failed')

    print('[resolve_coordinates] no coordinates resolved — distance ranking disabled')
    return MN_DEFAULT_LOCATION[1], MN_DEFAULT_LOCATION[2]


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.get('/')
def index(): return send_from_directory(app.static_folder or '.', 'index.html')

@app.get('/api/geocode')
def api_geocode():
    """Resolve a ?q= city/ZIP string to {lat, lng, location}."""
    value = request.args.get('q', '')
    lat, lng, label = resolve_location_text(value)
    if lat is None or lng is None:
        return jsonify({'location': None, 'error': 'Enter a valid Minnesota town or ZIP code.'}), 400
    return jsonify({'location': label, 'lat': lat, 'lng': lng})

@app.get('/api/locations')
def locations():
    """Return every town/ZIP pair represented by a daycare in the database."""
    with get_db() as c:
        rows = c.execute('''
            SELECT city, zip, COUNT(*) AS providers
            FROM providers
            WHERE TRIM(city)<>'' AND TRIM(zip)<>''
            GROUP BY city, zip
            ORDER BY city, zip
        ''').fetchall()
    return jsonify([dict(row) for row in rows])


@app.get('/api/ai-search')
def ai_search():
    """
    Natural-language provider search.  Query parameters:
      q    — free-text search query (e.g. "infant care near Minneapolis open Monday")
      lat  — caller latitude (from browser geolocation, optional)
      lng  — caller longitude (from browser geolocation, optional)

    Processing pipeline:
      1. Parse intent from query text via rule_parse_query().
      2. Resolve a search-origin (lat/lng) via resolve_coordinates().
      3. Load all providers from the DB.
      4. Apply hard filters: day hours, open time, special-need intent matches.
      5. Apply distance radius filter (MAX_SEARCH_MILES) when a location is known.
      6. Score and sort results.
      7. Return the top 15.

    Returns JSON with: query, parsed, resolved_location, location_fallback,
    has_distance_origin, requested_day, requested_time, hours_sort, radius_miles,
    recommendations, notice.
    """
    text = request.args.get('q', '').strip()
    print(f'\n{"="*60}')
    print(f'[ai_search] ▶ query={text!r}')
    if not text:
        return jsonify({'query':'','recommendations':[],'requested_day':'','requested_time':''})

    # Parse optional browser-supplied GPS coordinates.
    try:    caller_lat = float(request.args['lat']) if request.args.get('lat') else None
    except: caller_lat = None
    try:    caller_lng = float(request.args['lng']) if request.args.get('lng') else None
    except: caller_lng = None
    print(f'[ai_search] caller coords: lat={caller_lat}, lng={caller_lng}')

    print('[ai_search] parsing with local Minnesota resolver')
    parsed = rule_parse_query(text)

    # When no location is parsed, default to the centre of Minnesota so the
    # distance ranking still works (all providers appear, sorted by state centroid).
    if not parsed.get('city') and not parsed.get('zip'):
        parsed['city'] = MN_DEFAULT_LOCATION[0]
        parsed['location_fallback'] = True

    print(f'[ai_search] FINAL parsed={parsed}')

    day          = parsed.get('day') or ''
    need         = parsed.get('needs') or []
    age_group    = parsed.get('age_group') or ''
    req_time_str = parsed.get('open_time') or ''
    req_time, req_time_text = parse_time(req_time_str) if req_time_str else (None, '')
    hours_sort = parsed.get('hours_sort') or ''
    print(f'[ai_search] day={day!r} need={need} age_group={age_group!r} req_time={req_time} req_time_text={req_time_text!r}')

    # ── Resolve location ──
    print('[ai_search] --- step 3: resolve_coordinates ---')
    lat, lng = resolve_coordinates(parsed, caller_lat, caller_lng)
    print(f'[ai_search] resolved coords: lat={lat}, lng={lng}')

    # Merge age_group into the need list so score_intents checks it as well.
    if age_group and age_group not in need:
        need = need + [age_group]
        print(f'[ai_search] merged age_group into need: {need}')

    # ── Load all providers (raw_search included for intent scoring) ──
    with get_db() as c:
        rows = c.execute('SELECT ' + SELECT_COLS + ', raw_search FROM providers').fetchall()
    print(f'[ai_search] loaded {len(rows)} total providers from DB')

    if rows:
        sample = dict(rows[0])
        print(f'[ai_search] sample provider[0]: name={sample.get("name")!r} city={sample.get("city")!r} address={sample.get("address")!r} zip={sample.get("zip")!r} lat={sample.get("lat")} lng={sample.get("lng")}')

    out = []
    filtered_day = 0
    filtered_time = 0
    filtered_need = 0
    filtered_radius = 0
    # has_explicit_location controls whether the MAX_SEARCH_MILES radius cap is applied.
    has_explicit_location = bool((parsed.get('city') or parsed.get('zip')) and not parsed.get('location_fallback')) or caller_lat is not None and caller_lng is not None

    for r in rows:
        x = dict(r)

        # Hard filter: provider must have hours on the requested day.
        if day and not x.get(day):
            filtered_day += 1
            continue

        # Hard filter: provider must cover the requested time on the relevant day(s).
        if req_time is not None:
            cols_to_check = [x[day]] if day else [x[d] for d in DAYS]
            if not any(time_in_hours(v, req_time) for v in cols_to_check):
                filtered_time += 1
                continue

        # Hard filter: if special needs / language / age intents were requested,
        # the provider's features or raw_search must confirm the match.
        matched_intents = score_intents(x.get('features',''), x.get('raw_search',''), need) if need else []
        if need and not matched_intents:
            filtered_need += 1
            continue

        # Compute distance; apply radius cap when a location was explicitly given.
        if lat is not None and lng is not None and x.get('lat') and x.get('lng'):
            x['distance_miles'] = round(distance_miles(lat, lng, x['lat'], x['lng']), 1)
        else:
            x['distance_miles'] = None
        if has_explicit_location and (x['distance_miles'] is None or x['distance_miles'] > MAX_SEARCH_MILES):
            filtered_radius += 1
            continue

        # ── Composite score (0–1) ──
        score = 0.0
        reasons = []

        if x['distance_miles'] is not None:
            # Proximity: 0.65 weight, linearly decays to 0 at 50 miles.
            score += max(0, 1 - x['distance_miles'] / 50) * 0.65
            reasons.append(f"{x['distance_miles']} miles away")

        if day:
            score += 0.15
            reasons.append(f"open {day.title()}")

        if req_time is not None:
            score += 0.20
            reasons.append(f"open at {req_time_text}")

        if matched_intents:
            # Cap at 0.20 so intent matches don't overwhelm proximity.
            score += min(0.20, 0.08 * len(matched_intents))
            reasons.append('supports ' + ', '.join(matched_intents))

        # Compute opening/closing/total-open minutes for hours-sort modes.
        schedule_values = [x[day]] if day else [x[d] for d in DAYS]
        schedule_bounds = [bound for value in schedule_values for bound in hour_bounds(value)]
        x['opening_minutes'] = min((bound[0] for bound in schedule_bounds), default=None)
        x['closing_minutes'] = max((bound[1] for bound in schedule_bounds), default=None)
        x['open_minutes'] = sum(max(0, end - start) for start, end in schedule_bounds)
        if hours_sort == 'opens_earliest' and x['opening_minutes'] is not None:
            reasons.append('opens earliest first')
        elif hours_sort == 'closes_latest' and x['closing_minutes'] is not None:
            reasons.append('closes latest first')
        elif hours_sort == 'longest' and x['open_minutes']:
            reasons.append('longest recorded hours')

        # Rating contributes a small boost (0–0.10).
        score += rating_score(x.get('rating','')) * 0.10

        x['ai_score']      = round(score, 3)
        x['match_reasons'] = reasons
        out.append(x)

    print(f'[ai_search] filtering: {filtered_day} dropped by day, {filtered_time} by time, {filtered_need} by need, {filtered_radius} beyond {MAX_SEARCH_MILES} miles')
    print(f'[ai_search] {len(out)} providers passed all filters')
    print(f'[ai_search] distance_miles is None for {sum(1 for x in out if x["distance_miles"] is None)} providers')

    # ── Sort ──
    if hours_sort == 'opens_earliest':
        out.sort(key=lambda x: (x['opening_minutes'] is None, x['opening_minutes'] if x['opening_minutes'] is not None else 9999, x['distance_miles'] is None, x['distance_miles'] if x['distance_miles'] is not None else 9999, x['name']))
    elif hours_sort == 'closes_latest':
        out.sort(key=lambda x: (x['closing_minutes'] is None, -(x['closing_minutes'] or 0), x['distance_miles'] is None, x['distance_miles'] if x['distance_miles'] is not None else 9999, x['name']))
    elif hours_sort == 'longest':
        out.sort(key=lambda x: (-x['open_minutes'], x['distance_miles'] is None, x['distance_miles'] if x['distance_miles'] is not None else 9999, x['name']))
    else:
        # Default: closest first, then highest score, then alphabetical.
        out.sort(key=lambda x: (x['distance_miles'] is None, x['distance_miles'] if x['distance_miles'] is not None else 9999, -x['ai_score'], x['name']))

    top = out[:15]
    if top:
        print(f'[ai_search] top result: name={top[0].get("name")!r} city={top[0].get("city")!r} dist={top[0].get("distance_miles")} score={top[0].get("ai_score")}')
    print(f'[ai_search] ◀ returning {len(top)} recommendations')

    return jsonify({
        'query':               text,
        'parsed':              parsed,
        'resolved_location':   parsed.get('city') or parsed.get('zip') or MN_DEFAULT_LOCATION[0],
        'location_fallback':   bool(parsed.get('location_fallback')),
        'has_distance_origin': lat is not None and lng is not None,
        'requested_day':       day,
        'requested_time':      req_time_text,
        'hours_sort':          hours_sort,
        'radius_miles':        MAX_SEARCH_MILES if has_explicit_location else None,
        'recommendations':     top,
        'notice':              "Results are based on provider directory records only. Contact each provider to confirm availability, schedule, and accommodations."
    })

# ── Server startup ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    host  = os.getenv('FLASK_HOST', '0.0.0.0')
    port  = int(os.getenv('FLASK_PORT', '5000'))
    debug = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(host=host, port=port, debug=True)
