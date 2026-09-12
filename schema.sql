CREATE TABLE IF NOT EXISTS providers (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 type TEXT,
 rating TEXT,
 license TEXT,
 address TEXT,
 city TEXT,
 state TEXT,
 zip TEXT,
 county TEXT,
 prog_type TEXT,
 lat REAL,
 lng REAL,
 phone TEXT,
 email TEXT,
 website TEXT,
 features TEXT,
 ages_served TEXT,
 raw_search TEXT,
 age_range TEXT,
 age_openings TEXT,
 monday TEXT,
 tuesday TEXT,
 wednesday TEXT,
 thursday TEXT,
 friday TEXT,
 saturday TEXT,
 sunday TEXT,
 hours_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_name ON providers(name);
CREATE INDEX IF NOT EXISTS idx_provider_type ON providers(type);
CREATE INDEX IF NOT EXISTS idx_provider_county ON providers(county);
CREATE INDEX IF NOT EXISTS idx_provider_rating ON providers(rating);
CREATE INDEX IF NOT EXISTS idx_provider_city ON providers(city);
