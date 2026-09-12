import csv, sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / 'data' / 'childcare.db'
CSV = BASE / 'hours.csv'

def norm(v):
    return ' '.join(str(v or '').strip().lower().split())

if not DB.exists():
    raise SystemExit('childcare.db does not exist. Run importer.py first.')

with sqlite3.connect(DB) as conn, CSV.open(newline='', encoding='utf-8-sig') as f:
    rows = list(csv.DictReader(f))
    updated = 0
    for r in rows:
        name, city, state = norm(r['name']), norm(r['city']), norm(r['state'])
        match = conn.execute(
            'SELECT id FROM providers WHERE lower(trim(name))=? AND lower(trim(city))=? AND lower(trim(state))=? LIMIT 1',
            (name, city, state)
        ).fetchone()
        if not match:
            continue
        conn.execute('''UPDATE providers SET
            monday=?,tuesday=?,wednesday=?,thursday=?,friday=?,saturday=?,sunday=?,hours_source=?
            WHERE id=?''', (
            r['monday'], r['tuesday'], r['wednesday'], r['thursday'], r['friday'],
            r['saturday'], r['sunday'], r['hours_source'], match[0]
        ))
        updated += 1
    conn.commit()
print(f'Applied hours to {updated} providers from hours.csv.')
