# Minnesota Child Care Finder

The importer pulls provider data from one of three sources (default: Parent Aware) and writes a local SQLite database used by the app.

## Run on Windows

```powershell
cd C:\path\to\minnesota_childcare_finder
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python importer.py                   # default: Parent Aware (explicit hours only)
python importer.py --source arcgis   # MN ArcGIS state dataset
python importer.py --source arcgis-osm  # ArcGIS + OpenStreetMap hours/contacts
python app.py
```

If PowerShell blocks activation, use:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

The server listens on all network interfaces by default. Open `http://127.0.0.1:5000` on the same computer, or use the computer's LAN address, such as `http://192.168.1.25:5000`, from another device on the same network.

To use a public internet address, allow inbound TCP port 5000 through Windows Firewall and configure port forwarding on the router to this computer. Do not enable Flask debug mode on a public address.

The bind address, port, and debug mode can be overridden with `FLASK_HOST`, `FLASK_PORT`, and `FLASK_DEBUG`.

## AI-style daycare search

The site now has a natural-language search box. Examples:

- `Find the closest daycare for a child with special needs that is open Monday`
- `Find infant care near me`
- `Find a daycare that mentions autism accommodations`
- `Find Saturday care`

The matcher searches explicit provider-directory fields for terms related to special needs, autism, behavioral needs, medical needs, sensory needs, languages, infant/toddler/preschool care, and similar requests. It never claims that a provider can accommodate a need unless the provider data contains a relevant match.

For location-based ranking, click **Use my location** or allow the AI search to request browser location. Location is used only for the search and is not stored by this app.

## Important limitation about special-needs matching

A text match is **not a guarantee of accommodation**. For an individual child's needs, the family should contact the provider and confirm the specific accommodation, staffing, accessibility, medical/behavioral support, and enrollment situation.

## Important limitation about hours

Parent Aware's public family materials say families can search by hours of operation, and its provider Update Tool allows providers to update hours. However, the location API used by this project may not expose hours for every provider. This project therefore excludes providers with no explicit hours in the imported API response rather than displaying made-up or inferred hours.

## Data sources

| `--source` | Description |
|---|---|
| `parentaware` *(default)* | Parent Aware provider directory — only providers with explicit hours |
| `arcgis` | MN Dept of Education ArcGIS dataset — all licensed providers, hours if exposed |
| `arcgis-osm` | ArcGIS dataset enriched with OpenStreetMap hours and contact fields |

Parent Aware provider directory: https://www.parentaware.org/

The location endpoint used by the importer is:
`https://www.parentaware.org/wp-json/pa/v3/providers/list/{lat}/{lng}`

This endpoint is an implementation detail of the Parent Aware site rather than a formal public API guarantee, so it may change.
