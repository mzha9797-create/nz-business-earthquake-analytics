
PRAGMA foreign_keys=ON;
CREATE TABLE dim_region(
 region_code TEXT PRIMARY KEY CHECK(length(region_code)=2),
 region_name TEXT NOT NULL UNIQUE, land_area_km2 REAL CHECK(land_area_km2>=0),
 boundary_year INTEGER NOT NULL CHECK(boundary_year=2025));
CREATE TABLE fact_earthquake(
 public_id TEXT PRIMARY KEY, event_type TEXT NOT NULL CHECK(event_type='earthquake'),
 origin_utc TEXT NOT NULL, modified_utc TEXT NOT NULL,
 longitude REAL NOT NULL CHECK(longitude BETWEEN 165 AND 180),
 latitude REAL NOT NULL CHECK(latitude BETWEEN -48 AND -33),
 magnitude REAL NOT NULL CHECK(magnitude BETWEEN -3 AND 10),
 depth_km REAL NOT NULL CHECK(depth_km BETWEEN -10 AND 800),
 magnitude_type TEXT, depth_type TEXT, evaluation_method TEXT, evaluation_status TEXT,
 evaluation_mode TEXT, earth_model TEXT, used_phase_count INTEGER,
 used_station_count INTEGER, magnitude_station_count INTEGER,
 minimum_distance REAL, azimuthal_gap REAL, origin_error REAL, magnitude_uncertainty REAL,
 event_year INTEGER NOT NULL CHECK(event_year BETWEEN 2020 AND 2024),
 event_month TEXT NOT NULL,
 region_code TEXT NOT NULL REFERENCES dim_region(region_code),
 mapping_status TEXT NOT NULL CHECK(mapping_status IN ('matched','unmapped')),
 source_file TEXT NOT NULL);
CREATE INDEX idx_quake_region_magnitude_depth ON fact_earthquake(region_code,magnitude,depth_km);
CREATE INDEX idx_quake_origin ON fact_earthquake(origin_utc);
CREATE TABLE fact_business(
 region_code TEXT NOT NULL REFERENCES dim_region(region_code),
 reference_year INTEGER NOT NULL CHECK(reference_year BETWEEN 2016 AND 2025),
 geographic_units INTEGER NOT NULL CHECK(geographic_units>=0),
 employees INTEGER NOT NULL CHECK(employees>=0),
 source_sheet TEXT NOT NULL DEFAULT 'Table 2',
 PRIMARY KEY(region_code,reference_year));
CREATE TABLE published_national_business(
 reference_year INTEGER PRIMARY KEY, geographic_units INTEGER NOT NULL,
 employees INTEGER NOT NULL);
CREATE TABLE etl_audit(
 run_id TEXT PRIMARY KEY, completed_utc TEXT NOT NULL,
 raw_records INTEGER NOT NULL, accepted_records INTEGER NOT NULL,
 duplicate_records INTEGER NOT NULL, rejected_records INTEGER NOT NULL,
 unmapped_records INTEGER NOT NULL, elapsed_seconds REAL NOT NULL,
 source_manifest_sha256 TEXT NOT NULL);
CREATE VIEW v_region_screening AS
WITH q AS (SELECT region_code,COUNT(*) event_count,
 SUM(CASE WHEN magnitude>=4 THEN 1 ELSE 0 END) m4_events,
 SUM(CASE WHEN magnitude>=4 AND depth_km<=40 THEN 1 ELSE 0 END) shallow_m4_events,
 MAX(magnitude) max_magnitude FROM fact_earthquake GROUP BY region_code)
SELECT r.region_code,r.region_name,r.land_area_km2,
 COALESCE(q.event_count,0) event_count,COALESCE(q.m4_events,0) m4_events,
 COALESCE(q.shallow_m4_events,0) shallow_m4_events,q.max_magnitude,
 b.geographic_units business_units_2025,b.employees employees_2025,
 b.geographic_units-p.geographic_units business_change_2024_2025,
 1.0*(b.geographic_units-p.geographic_units)/NULLIF(p.geographic_units,0) business_change_rate,
 b.reference_year business_reference_year
FROM dim_region r LEFT JOIN q USING(region_code)
LEFT JOIN fact_business b ON b.region_code=r.region_code AND b.reference_year=2025
LEFT JOIN fact_business p ON p.region_code=r.region_code AND p.reference_year=2024;
CREATE VIEW v_monthly_seismicity AS
SELECT region_code,event_month,COUNT(*) event_count,
 SUM(CASE WHEN magnitude>=4 THEN 1 ELSE 0 END) m4_events,
 SUM(CASE WHEN magnitude>=4 AND depth_km<=40 THEN 1 ELSE 0 END) shallow_m4_events
FROM fact_earthquake GROUP BY region_code,event_month;
