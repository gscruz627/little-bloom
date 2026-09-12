"""
Minnesota Child Care Finder — importer
Usage:
    python importer.py                        # default: parentaware
    python importer.py --source parentaware
    python importer.py --source arcgis
    python importer.py --source arcgis-osm

Sources:
  parentaware  — Parent Aware provider directory. Only imports providers that
                 return at least one explicit hours value from the API.
  arcgis       — Minnesota Dept of Education ArcGIS MapServer (4 licensed-care
                 layers). Includes all providers; hours populated only when the
                 layer exposes them.
  arcgis-osm   — Same as arcgis, but enriches each record with hours and contact
                 details matched from OpenStreetMap via the Overpass API.
"""
import argparse, json, math, re, sqlite3, time
from pathlib import Path
import requests

# Just define the base directory
BASE = Path(__file__).resolve().parent

# Where is the database
DB   = BASE / 'data' / 'childcare.db'

# Canonical day order used throughout — keys in the providers table follow this list.
DAYS = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']


# ── shared helpers ────────────────────────────────────────────────────────────

def clean(v):
    """Return a stripped string, or '' for None/dict/list values."""
    if v is None: return ''
    if isinstance(v, (dict, list)): return ''
    return str(v).strip()

def norm(s):
    """Lowercase, collapse non-alphanumeric runs to spaces. Used for dedup keys."""
    s = (s or '').lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def norm_key(k):
    """Strip everything but lowercase letters and digits from a field name."""
    return re.sub(r'[^a-z0-9]', '', str(k).lower())

def write_db(records, cols):
    """
    Drop and recreate childcare.db, apply schema.sql, then insert every record.

    cols    — ordered list of column names to write; must be a subset of the
              providers table columns (or new columns that will be ALTER-ed in).
    records — list of dicts; missing keys default to ''.
    Returns the count of rows actually written (after deduplication by name/city/type).
    """
    DB.parent.mkdir(exist_ok=True)
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.executescript((BASE / 'schema.sql').read_text(encoding='utf-8'))

    # Add any columns that exist in records but are not yet in the schema.
    existing = {row[1] for row in conn.execute('PRAGMA table_info(providers)')}
    for col in cols:
        if col not in existing:
            conn.execute(f'ALTER TABLE providers ADD COLUMN {col} TEXT')

    placeholders = ','.join('?' for _ in cols)
    sql = f"INSERT INTO providers ({','.join(cols)}) VALUES ({placeholders})"

    seen = set(); count = 0
    for r in records:
        # Deduplicate across sources: same normalised name + city + type = same provider.
        key = (norm(r.get('name')), norm(r.get('city')), norm(r.get('type')))
        if key in seen:
            continue
        seen.add(key)
        conn.execute(sql, [r.get(c, '') for c in cols])
        count += 1

    conn.commit(); conn.close()
    return count


# ── ArcGIS source ─────────────────────────────────────────────────────────────

ARCGIS_URL = 'https://api.education.mn.gov/arcgis/rest/services/eclds/early_care_ed_sites/MapServer'

# Layer IDs and human-readable type labels for the four licensed-care layers.
ARCGIS_LAYERS = [
    (2, 'Head Start / Early Head Start'),
    (3, 'Licensed Child Care Center'),
    (4, 'Licensed Family Child Care'),
    (6, 'Tribally Licensed Child Care'),
]

# The state ArcGIS dataset has renamed hours fields several times.
# Each day maps to a prioritised list of (open_field, close_field) pairs to try;
# a None close_field means the open field already contains the full hours string.
HOUR_ALIASES = {
    'monday':    [('MON_OPEN','MON_CLOSE'),('MONDAY_OPEN','MONDAY_CLOSE'),('MON_START','MON_END'),('MON_HOURS',None),('HOURS_MON',None)],
    'tuesday':   [('TUE_OPEN','TUE_CLOSE'),('TUESDAY_OPEN','TUESDAY_CLOSE'),('TUE_START','TUE_END'),('TUE_HOURS',None),('HOURS_TUE',None)],
    'wednesday': [('WED_OPEN','WED_CLOSE'),('WEDNESDAY_OPEN','WEDNESDAY_CLOSE'),('WED_START','WED_END'),('WED_HOURS',None),('HOURS_WED',None)],
    'thursday':  [('THU_OPEN','THU_CLOSE'),('THURSDAY_OPEN','THURSDAY_CLOSE'),('THU_START','THU_END'),('THU_HOURS',None),('HOURS_THU',None)],
    'friday':    [('FRI_OPEN','FRI_CLOSE'),('FRIDAY_OPEN','FRIDAY_CLOSE'),('FRI_START','FRI_END'),('FRI_HOURS',None),('HOURS_FRI',None)],
    'saturday':  [('SAT_OPEN','SAT_CLOSE'),('SATURDAY_OPEN','SATURDAY_CLOSE'),('SAT_START','SAT_END'),('SAT_HOURS',None),('HOURS_SAT',None)],
    'sunday':    [('SUN_OPEN','SUN_CLOSE'),('SUNDAY_OPEN','SUNDAY_CLOSE'),('SUN_START','SUN_END'),('SUN_HOURS',None),('HOURS_SUN',None)],
}

def _arcgis_norm_keys(props):
    """Upper-case all property keys and replace non-alphanumeric chars with '_'."""
    return {re.sub(r'[^A-Z0-9]', '_', str(k).upper()): v for k, v in props.items()}

def _arcgis_format_hours(props):
    """
    Walk HOUR_ALIASES for each day and extract the best hours string found.
    Returns (dict of day→str, found: bool).  found is True if any day had a value.
    """
    p = _arcgis_norm_keys(props)
    out = {}; found = False
    for day in DAYS:
        value = ''
        for open_key, close_key in HOUR_ALIASES[day]:
            if open_key in p and close_key and close_key in p:
                # Prefer open+close pair — format as "HH:MM - HH:MM".
                a, b = clean(p[open_key]), clean(p[close_key])
                if a or b:
                    value = f'{a} - {b}'.strip(' -')
                    found = True; break
            elif open_key in p:
                # Single field already contains the full hours string.
                v = clean(p[open_key])
                if v:
                    value = v; found = True; break
        out[day] = value
    return out, found

def _arcgis_fetch_layer(lid, typ):
    """
    Generator that pages through all features in a single ArcGIS MapServer layer.
    Yields (feature_geojson_dict, typ) for each feature.
    Uses 2000-record pages with a short sleep between requests to be polite.
    """
    off = 0
    while True:
        r = requests.get(
            f'{ARCGIS_URL}/{lid}/query',
            params={
                'where': '1=1', 'outFields': '*', 'returnGeometry': 'true',
                'outSR': '4326', 'f': 'geojson',
                'resultRecordCount': 2000, 'resultOffset': off,
            }, timeout=60
        )
        r.raise_for_status()
        fs = r.json().get('features', [])
        if not fs:
            break
        for f in fs:
            yield f, typ
        if len(fs) < 2000:
            break           # last page — no need for another round-trip
        off += 2000
        time.sleep(.1)      # avoid hammering the API

def run_arcgis():
    """
    Import all providers from the MN ArcGIS state dataset.
    Hours are written only when the layer actually exposes them; records without
    hours are still imported (hours columns left empty).
    """
    records = []
    for lid, typ in ARCGIS_LAYERS:
        print('Downloading', typ)
        for f, t in _arcgis_fetch_layer(lid, typ):
            a  = f.get('properties') or {}
            co = (f.get('geometry') or {}).get('coordinates') or []
            if len(co) < 2:
                continue    # skip features with no usable geometry
            hours, has_hours = _arcgis_format_hours(a)
            records.append(dict(
                name     = clean(a.get('NAME')) or 'Unnamed program',
                type     = t,
                rating   = clean(a.get('RATING')),
                license  = clean(a.get('DHS_License')),
                city     = clean(a.get('CITY')),
                state    = clean(a.get('STATE')) or 'MN',
                zip      = clean(a.get('ZIP')),
                county   = clean(a.get('COUNTY')),
                prog_type= clean(a.get('PROG_TYPE')),
                lat      = float(co[1]),  # GeoJSON order: [lng, lat]
                lng      = float(co[0]),
                hours_source = 'Minnesota ArcGIS dataset' if has_hours else '',
                **hours,
            ))
    cols = ['name','type','rating','license','city','state','zip','county','prog_type','lat','lng'] + DAYS + ['hours_source']
    total = write_db(records, cols)
    with_hours = sum(1 for r in records if any(r.get(d) for d in DAYS))
    print(f'Imported {total} providers; {with_hours} had hours in the source dataset.')


# ── ArcGIS + OpenStreetMap source ─────────────────────────────────────────────

# Three Overpass API mirrors tried in order; falls back gracefully if all fail.
OVERPASS_URLS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass.private.coffee/api/interpreter',
]

def _haversine(lat1, lon1, lat2, lon2):
    """Return the great-circle distance in miles between two lat/lng points."""
    r = 3958.8  # Earth radius in miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))

def _parse_osm_hours(h):
    """
    Convert an OSM opening_hours string into a per-day dict.

    Handles the most common patterns:
      - "Mo-Fr 07:00-18:00; Sa 08:00-12:00"
      - "monday-friday: 07:00-18:00"
      - "Mo-Fr,Sa 07:00-18:00"
      - Plain strings with no day prefix (applied to all days as-is).
    Complex/holiday expressions are left intact rather than guessed.
    """
    if not h:
        return {d: '' for d in DAYS}
    out = {d: '' for d in DAYS}
    for p in [x.strip() for x in h.strip().split(';') if x.strip()]:
        if ':' not in p:
            continue
        lhs, rhs = p.split(':', 1)
        rhs = rhs.strip()
        names = [x.strip().lower() for x in lhs.split(',')]
        expanded = []
        for x in names:
            if '-' in x:
                # Range like "monday-friday" or "mo-fr"
                a, b = [z.strip().lower() for z in x.split('-', 1)]
                if a in DAYS and b in DAYS:
                    ia, ib = DAYS.index(a), DAYS.index(b)
                    expanded += DAYS[ia:ib+1] if ia <= ib else DAYS[ia:] + DAYS[:ib+1]
            elif x in DAYS:
                expanded.append(x)
        # Handle common OSM abbreviations that don't fully spell out day names.
        if not expanded and lhs.lower() in ('mo-fr', 'mon-fri', 'monday-friday'):
            expanded = DAYS[:5]
        for d in expanded:
            out[d] = rhs
    if not any(out.values()):
        # No day prefix found — treat the whole string as same hours every day.
        for d in DAYS:
            out[d] = h
    return out

def _arcgis_state_records():
    """
    Fetch all records from the ArcGIS layers and return them as a list of dicts,
    pre-populated with empty contact and hours fields ready for OSM enrichment.
    """
    records = []
    for layer_id, ptype in dict(ARCGIS_LAYERS).items():
        offset = 0
        while True:
            r = requests.get(
                f'{ARCGIS_URL}/{layer_id}/query',
                params={
                    'where': '1=1', 'outFields': '*', 'returnGeometry': 'true',
                    'outSR': '4326', 'f': 'geojson',
                    'resultOffset': offset, 'resultRecordCount': 2000,
                }, timeout=60
            )
            r.raise_for_status()
            feats = r.json().get('features', [])
            if not feats:
                break
            for f in feats:
                prop   = f.get('properties') or {}
                coords = (f.get('geometry') or {}).get('coordinates') or []
                if len(coords) < 2:
                    continue
                lng, lat = coords[:2]   # GeoJSON: [longitude, latitude]
                records.append({
                    'name'     : prop.get('NAME') or prop.get('Provider_Name') or 'Unnamed provider',
                    'type'     : ptype,
                    'rating'   : prop.get('RATING') or '',
                    'license'  : prop.get('DHS_License') or '',
                    'city'     : prop.get('CITY') or '',
                    'state'    : prop.get('STATE') or 'MN',
                    'zip'      : prop.get('ZIP') or '',
                    'county'   : prop.get('COUNTY') or '',
                    'prog_type': prop.get('PROG_TYPE') or '',
                    'lat'      : lat, 'lng': lng,
                    # Contact fields start empty; OSM enrichment fills them in if matched.
                    'phone': '', 'email': '', 'website': '',
                    'address'  : prop.get('ADDRESS') or prop.get('STREET') or '',
                    # Concatenate several potential feature/description fields.
                    'features' : ' '.join(str(prop.get(k, '')) for k in ('SPECIAL_NEEDS','FEATURES','SERVICES','DESCRIPTION')),
                    'ages_served': str(prop.get('AGES_SERVED') or prop.get('AGES') or ''),
                    # Store the raw properties blob for full-text search in the app.
                    'raw_search': json.dumps(prop, ensure_ascii=False),
                    **{d: '' for d in DAYS}, 'hours_source': '',
                })
            if len(feats) < 2000:
                break
            offset += len(feats)
    return records

def _osm_records():
    """
    Query the Overpass API for Minnesota childcare nodes/ways/relations that have
    an opening_hours tag.  The bounding box (43.4–49.4°N, 97.3–89.4°W) covers the
    full state.  Returns a list of dicts with name, lat, lng, contact fields,
    and a parsed per-day hours dict.
    Falls back gracefully — returns [] if all mirrors are unreachable.
    """
    q = (
        '[out:json][timeout:180];('
        # Childcare and kindergarten with hours anywhere in Minnesota.
        'nwr["amenity"="childcare"]["opening_hours"](43.4,-97.3,49.4,-89.4);'
        'nwr["amenity"="kindergarten"]["opening_hours"](43.4,-97.3,49.4,-89.4);'
        # ISCED level 0 = early childhood education (pre-primary).
        'nwr["amenity"="school"]["isced:level"~"(^|;)0(;|$)"]["opening_hours"](43.4,-97.3,49.4,-89.4);'
        ');out center tags;'
    )
    for url in OVERPASS_URLS:
        try:
            r = requests.post(url, data=q, timeout=180, headers={'User-Agent': 'MinnesotaChildCareFinder/1.0'})
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            print(f'OSM query failed at {url}: {e}')
    else:
        print('WARNING: OSM hours/contact enrichment unavailable. Continuing with state data.')
        return []

    out = []
    for el in data.get('elements', []):
        tags = el.get('tags') or {}
        # Ways/relations expose their centroid under 'center'; nodes use top-level lat/lon.
        lat  = el.get('lat') or (el.get('center') or {}).get('lat')
        lng  = el.get('lon') or (el.get('center') or {}).get('lon')
        if lat is None or lng is None or not tags.get('name'):
            continue
        hours_str = tags.get('opening_hours', '').strip()
        if not hours_str:
            continue
        out.append({
            'name'   : tags.get('name'),
            'lat'    : float(lat), 'lng': float(lng),
            # OSM contact fields can be stored under contact:* or bare keys.
            'phone'  : tags.get('contact:phone') or tags.get('phone') or '',
            'email'  : tags.get('contact:email') or tags.get('email') or '',
            'website': tags.get('contact:website') or tags.get('website') or '',
            'hours'  : _parse_osm_hours(hours_str),
        })
    return out

def _best_osm_match(rec, osm):
    """
    Find the best-matching OSM record for a state provider record.

    Scoring (higher is better):
      +100  exact normalised name match
      +60   one name is a substring of the other
      +25+  token overlap (at least 2 shared words)
      +0–30 proximity bonus (closer = higher, up to 0.35 miles)

    Returns the OSM record if the top candidate scores ≥ 45, otherwise None.
    The 0.35-mile radius pre-filter prevents false matches between nearby providers.
    """
    rn = norm(rec['name'])
    candidates = []
    for o in osm:
        dist = _haversine(rec['lat'], rec['lng'], o['lat'], o['lng'])
        if dist > 0.35:
            continue    # too far away to plausibly be the same location
        on    = norm(o['name'])
        score = 0
        if rn == on: score += 100
        elif rn and on and (rn in on or on in rn): score += 60
        else:
            overlap = len(set(rn.split()) & set(on.split()))
            if overlap >= 2: score += 25 + overlap * 5
        score += max(0, 30 - dist * 60)   # up to 30 extra points for proximity
        candidates.append((score, dist, o))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, _, o = candidates[0]
    return o if score >= 45 else None

def _enrich_with_osm(records, osm):
    """
    For each state record, attempt to find a matching OSM record and copy over
    any contact details and hours that the state data is missing.
    hours_source is updated to 'OpenStreetMap' when hours are applied.
    Returns the count of records that were enriched.
    """
    matched = 0
    for r in records:
        o = _best_osm_match(r, osm)
        if not o:
            continue
        matched += 1
        # Copy contact fields only if the state record doesn't already have them.
        for k in ('phone', 'email', 'website'):
            if not r[k] and o.get(k):
                r[k] = o[k]
        # Overwrite hours only when the OSM record actually has hour data.
        if any(o['hours'].values()):
            for d in DAYS:
                if o['hours'].get(d):
                    r[d] = o['hours'][d]
            r['hours_source'] = 'OpenStreetMap'
    return matched

def run_arcgis_osm():
    """
    Import from the MN ArcGIS state dataset then enrich with OpenStreetMap data.
    All state providers are written to the DB regardless of whether they matched
    an OSM record.  hours_source is 'OpenStreetMap' for enriched rows, '' otherwise.
    """
    print('Downloading statewide Minnesota licensed child-care providers...')
    records = _arcgis_state_records()
    print(f'State providers: {len(records):,}')

    print('Downloading Minnesota OpenStreetMap childcare records with opening_hours/contact data...')
    osm = _osm_records()
    print(f'OSM providers with opening_hours: {len(osm):,}')

    matched = _enrich_with_osm(records, osm)
    print(f'Matched OSM hours/contact data to state providers: {matched:,}')

    cols = ['name','type','rating','license','address','city','state','zip','county','prog_type',
            'lat','lng','phone','email','website','features','ages_served','raw_search'] + DAYS + ['hours_source']
    total = write_db(records, cols)
    with_hours = sum(1 for r in records if any(r.get(d) for d in DAYS))
    print(f'Database providers: {total:,}')
    print(f'Providers with hours: {with_hours:,}')
    print(f'Database created at: {DB}')


# ── Parent Aware source ───────────────────────────────────────────────────────

PA_API = 'https://www.parentaware.org/wp-json/pa/v3/providers/list/{lat}/{lng}'

# The Parent Aware endpoint is location-based (returns providers near a point),
# so we query a regular grid that covers Minnesota.  Results overlap between grid
# cells and are deduplicated by license number or (name, address).
GRID_LAT = [43.6 + i * 0.35 for i in range(10)]   # 43.6°N – 46.75°N
GRID_LNG = [-96.9 + i * 0.55 for i in range(13)]  # 96.9°W – 89.7°W

# Reuse a session so TCP connections are pooled across the grid.
_session = requests.Session()
_session.headers.update({'User-Agent': 'Minnesota-Child-Care-Finder/2.0'})

def _flat(obj):
    """
    Recursively flatten a nested dict/list into a single-level dict.
    Keys are normalised with norm_key().  When two paths produce the same key,
    the first value wins (setdefault), preserving the shallowest/most canonical field.
    Both the short key and the full path key are stored so callers can use either.
    """
    out = {}
    def walk(x, prefix=''):
        if isinstance(x, dict):
            for k, v in x.items():
                nk = norm_key(k)
                if isinstance(v, (dict, list)):
                    walk(v, prefix + nk)
                else:
                    out.setdefault(nk, v)           # short key
                    out.setdefault(prefix + nk, v)  # full-path key
        elif isinstance(x, list):
            for item in x:
                if isinstance(item, dict): walk(item, prefix)
    walk(obj)
    return out

def _pick(d, *keys):
    """Return the first non-empty cleaned value found among the given keys."""
    for key in keys:
        if key in d and clean(d[key]): return clean(d[key])
    return ''

def _number(d, *keys):
    """Return the first key's value as a float, or None if none parse."""
    v = _pick(d, *keys)
    try: return float(v)
    except: return None

def _hour_value(d, day):
    """
    Extract a single day's hours from a flat provider dict.
    Tries composite 'dayhours' style keys first, then separate open/close keys.
    Returns a formatted string like "7:00 AM - 6:00 PM", or '' if not found.
    """
    abbr = {'monday':'mon','tuesday':'tue','wednesday':'wed','thursday':'thu',
            'friday':'fri','saturday':'sat','sunday':'sun'}[day]
    # Try keys that hold the full hours string for a day.
    candidates = [
        f'{day}hours', f'{abbr}hours', f'hours{day}', f'hours{abbr}',
        f'{day}operation', f'{abbr}operation', f'{day}hoursoperation',
    ]
    v = _pick(d, *candidates)
    if v: return v
    # Fall back to separate open/close fields.
    op = _pick(d, f'{day}open', f'{abbr}open', f'{day}start', f'{abbr}start')
    cl = _pick(d, f'{day}close', f'{abbr}close', f'{day}end', f'{abbr}end')
    return (op + (' - ' if op and cl else '') + cl).strip() if (op or cl) else ''

def _schedule_hours(raw):
    """
    Parse hours from the structured shifts.days block that Parent Aware returns.
    Each day may have multiple shift entries (e.g. split-day care);
    multiple shifts are joined with ' / '.
    Returns a dict of day→str; empty string means no hours data for that day.
    """
    days_data = ((raw.get('shifts') or {}).get('days') or {}) if isinstance(raw, dict) else {}
    out = {}
    for day in DAYS:
        # API may capitalise the day name ('Monday') or use lowercase ('monday').
        entries = days_data.get(day.title()) or days_data.get(day) or []
        if isinstance(entries, dict): entries = [entries]   # normalise single-entry case
        values = []
        for entry in entries:
            if not isinstance(entry, dict): continue
            start = clean(entry.get('startTime'))
            end   = clean(entry.get('endTime'))
            if start or end:
                values.append((start + (' - ' if start and end else '') + end).strip())
        out[day] = ' / '.join(values)
    return out

def _age_opening_values(raw):
    """
    Extract the human-readable age range label and a per-age-group vacancy summary.
    Returns (age_range: str, age_openings: str).
    age_openings is formatted as "Infant: 2; Toddler: 0" etc.
    """
    age_range = clean(((raw.get('ageRange') or {}).get('fullLabel')) if isinstance(raw, dict) else '')
    vacancies  = raw.get('vacancies') if isinstance(raw, dict) else {}
    parts = []
    if isinstance(vacancies, dict):
        for age, values in vacancies.items():
            if not isinstance(values, dict): continue
            total = values.get('Total')
            if total is not None and str(total).strip():
                parts.append(f'{age}: {total}')
    return age_range, '; '.join(parts)

def _pa_extract_records(payload):
    """
    Locate the list of provider dicts inside whatever JSON envelope the API returns.
    Parent Aware has used different wrapper keys over time; this tries the most
    common ones before falling back to a recursive name-key search.
    """
    if isinstance(payload, list): return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict): return []
    # Try well-known top-level wrapper keys.
    for key in ('providers','programs','results','data','items','locations'):
        val = payload.get(key)
        if isinstance(val, list): return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            # One level of nesting (e.g. {"providers": {"providers": [...]}}).
            for kk in ('providers','programs','results','items'):
                if isinstance(val.get(kk), list): return [x for x in val[kk] if isinstance(x, dict)]
    # Last resort: walk the whole payload looking for dicts that have a name-like key.
    found = []
    def walk(x):
        if isinstance(x, dict):
            if any(norm_key(k) in ('name','providername','programname','businessname') for k in x):
                found.append(x)
            else:
                for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(payload); return found

def _pa_normalize(raw):
    """
    Convert a raw Parent Aware provider dict to the canonical record format.
    Returns None if no usable name can be found (record is skipped).

    Hours are extracted from the structured shifts.days block first; flat-key
    fallback is used for any day not covered by the structured data.

    Features are assembled by scanning all keys for disability/inclusion/
    language/therapy terms — only values actually present in the data are included.
    """
    d    = _flat(raw)
    name = _pick(d, 'name','providername','programname','businessname','facilityname','organizationname')
    if not name: return None

    # Prefer structured shifts block; fill gaps from flat key patterns.
    hours = _schedule_hours(raw)
    for day in DAYS:
        if not hours[day]: hours[day] = _hour_value(d, day)

    # Collect special-needs / inclusion / language features without inferring anything.
    feature_keys = ('specialneeds','specialneed','disability','disabilities','accommodation','accommodations',
                    'inclusion','inclusive','individualized','iep','ifsp','behavioral','medicalneeds',
                    'medication','allergy','allergies','sensory','autism','developmental','therapy','therapies',
                    'transportation','languages','language','bilingual','multilingual')
    feature_parts = [str(v) for k, v in d.items() if any(t in k for t in feature_keys) and clean(v)]

    age_range, age_openings = _age_opening_values(raw)
    ages = _pick(d, 'agesserved','ages','agegroups','agegroup','servesages') or age_range

    return dict(
        name        = name,
        license     = _pick(d, 'license','licenseid','licensenumber','dhslicense','certificationnumber','providerid','id'),
        lat         = _number(d, 'lat','latitude','y'),
        lng         = _number(d, 'lng','lon','longitude','x'),
        address     = _pick(d, 'address','street','streetaddress','address1','locationaddress'),
        city        = _pick(d, 'city','town'),
        state       = _pick(d, 'state') or 'MN',
        zip         = _pick(d, 'zip','zipcode','postalcode'),
        county      = _pick(d, 'county','countyname'),
        phone       = _pick(d, 'phone','phonenumber','telephone','primaryphone'),
        email       = _pick(d, 'email','emailaddress'),
        website     = _pick(d, 'website','url','web','websiteurl'),
        rating      = _pick(d, 'rating','parentawarerating','starrating','stars'),
        type        = _pick(d, 'type','programtype','providertype','caretype','category'),
        prog_type   = _pick(d, 'progtype','program','servicetype','careprogram'),
        hours_source= 'Parent Aware provider directory',
        features    = ' | '.join(dict.fromkeys(feature_parts)),  # deduplicate while preserving order
        ages_served = ages,
        age_range   = age_range,
        age_openings= age_openings,
        # Flatten all scalar values into one searchable string for the app's text search.
        raw_search  = ' '.join(str(v) for v in d.values() if isinstance(v, (str, int, float))),
        **hours,
    )

def run_parentaware():
    """
    Import providers from the Parent Aware location API.

    Queries a statewide grid (130 points), deduplicates by license or (name, address),
    and writes only records that have at least one explicit hours value — the app
    relies on hours to answer "open on X day" queries, so providers without any
    hours data would never surface in those results.
    """
    DB.parent.mkdir(exist_ok=True)
    if DB.exists(): DB.unlink()
    with sqlite3.connect(DB) as c:
        c.executescript((BASE / 'schema.sql').read_text())
        seen = set(); total = 0; with_hours = 0
        for i, lat in enumerate(GRID_LAT):
            for lng in GRID_LNG:
                try:
                    r = _session.get(PA_API.format(lat=lat, lng=lng), timeout=45)
                    r.raise_for_status()
                    records = _pa_extract_records(r.json())
                except Exception as e:
                    print(f'Grid {lat:.2f},{lng:.2f} failed: {e}')
                    continue   # a single grid failure is non-fatal; continue with others

                for raw in records:
                    p = _pa_normalize(raw)
                    if not p: continue

                    # Deduplicate: prefer license-based key; fall back to name+address.
                    key = (p['license'].lower() if p['license'] else (p['name'].lower(), p.get('address','').lower()))
                    if key in seen: continue
                    seen.add(key)

                    # Skip records with no usable coordinates.
                    if p['lat'] is None or p['lng'] is None: continue

                    # Skip providers with no hours — we will not display made-up hours.
                    if not any(p[d] for d in DAYS): continue

                    with_hours += 1
                    c.execute(
                        '''INSERT INTO providers
                        (name,type,rating,license,address,city,state,zip,county,prog_type,phone,email,website,lat,lng,
                         monday,tuesday,wednesday,thursday,friday,saturday,sunday,hours_source,features,ages_served,age_range,age_openings,raw_search)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        tuple(p[k] for k in ['name','type','rating','license','address','city','state','zip','county',
                                             'prog_type','phone','email','website','lat','lng',
                                             *DAYS,'hours_source','features','ages_served','age_range','age_openings','raw_search'])
                    )
                    total += 1
                c.commit()   # commit after each grid cell so partial progress is saved
            print(f'Completed latitude band {i+1}/{len(GRID_LAT)}; {total} providers so far')

        print(f'Imported {total} unique Parent Aware providers; {with_hours} included hours.')
        if total == 0:
            print('No records returned. Parent Aware may have changed its API; inspect one endpoint response before retrying.')


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Import Minnesota child-care provider data.')
    parser.add_argument(
        '--source',
        choices=['parentaware', 'arcgis', 'arcgis-osm'],
        default='parentaware',
        help=(
            'parentaware  — Parent Aware directory (default, explicit hours only); '
            'arcgis       — MN ArcGIS state dataset; '
            'arcgis-osm   — MN ArcGIS state dataset enriched with OpenStreetMap hours/contacts'
        ),
    )
    args = parser.parse_args()
    if args.source == 'arcgis':
        run_arcgis()
    elif args.source == 'arcgis-osm':
        run_arcgis_osm()
    else:
        run_parentaware()

if __name__ == '__main__':
    main()
