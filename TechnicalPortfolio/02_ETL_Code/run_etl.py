"""Rebuild the earthquake and business warehouse from the supplied NZ snapshots.

Tested with Python 3.12.14. Install the pinned dependencies in requirements.txt.
The pipeline never multiplies regional business counts by earthquake records.
"""
from pathlib import Path
import argparse,csv,json,sqlite3,hashlib,datetime,time,os,math
import numpy as np
import openpyxl

SCHEMA='''
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
'''

def inside_ring(x,y,ring):
    """Vectorized even-odd ray crossing, using longitude in 0..360 degrees."""
    points=np.asarray(ring,float);px=np.where(points[:,0]<0,points[:,0]+360,points[:,0]);py=points[:,1]
    inside=np.zeros(x.shape,dtype=bool)
    j=len(points)-1
    for i in range(len(points)):
        if py[i]!=py[j]:
            hit=((py[i]>y)!=(py[j]>y)) & (x<(px[j]-px[i])*(y-py[i])/(py[j]-py[i])+px[i])
            inside^=hit
        j=i
    return inside

def assign_regions(lon,lat,features):
    """Preserve offshore/unmatched earthquakes as code 00 instead of discarding them."""
    lon=np.where(lon<0,lon+360,lon);codes=np.full(len(lon),'00',dtype='<U2');overlap=np.zeros(len(lon),int)
    for feature in features:
        g=feature['geometry'];polys=g['coordinates'] if g['type']=='MultiPolygon' else [g['coordinates']]
        mask=np.zeros(len(lon),bool)
        for polygon in polys:
            p=np.asarray(polygon[0],float);xs=np.where(p[:,0]<0,p[:,0]+360,p[:,0])
            idx=np.where((lon>=xs.min())&(lon<=xs.max())&(lat>=p[:,1].min())&(lat<=p[:,1].max()))[0]
            if not len(idx):continue
            current=inside_ring(lon[idx],lat[idx],polygon[0])
            for hole in polygon[1:]:current &= ~inside_ring(lon[idx],lat[idx],hole)
            mask[idx]|=current
        overlap+=mask
        codes[mask]=feature['properties']['REGC2025_V1_00']
    if np.any(overlap>1):raise ValueError('Boundary overlap would make the regional join ambiguous.')
    return codes

def write_csv(path,rows,fields=None):
    rows=list(rows);fields=fields or list(rows[0])
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def query_rows(con,sql):return [dict(r) for r in con.execute(sql)]

def run(root):
    started=time.perf_counter();root=Path(root).resolve()
    portfolio=root/'TechnicalPortfolio';raw=portfolio/'04_Data_Examples/Raw_Data';clean=portfolio/'04_Data_Examples/Clean_Data'
    dbdir=portfolio/'01_Database';evidence=portfolio/'04_Data_Examples/Verification'
    for p in (clean,dbdir,evidence,portfolio/'02_ETL_Code',portfolio/'03_Dashboards',portfolio/'05_Instructions',portfolio/'06_Demo_Videos'):p.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((raw/'source_manifest.json').read_text())
    for source in manifest['sources']:
        file=raw/source['file']
        if hashlib.sha256(file.read_bytes()).hexdigest()!=source['sha256']:raise ValueError('Raw snapshot hash mismatch: '+file.name)
    features=json.loads((raw/'statsnz_regions_2025_full.geojson').read_text())['features']
    assert len(features)==17
    regions=[{'region_code':'00','region_name':'Offshore or unmapped in study box','land_area_km2':None,'boundary_year':2025}]
    for f in features:
        p=f['properties'];regions.append({'region_code':p['REGC2025_V1_00'],'region_name':p['REGC2025_V1_00_NAME'],'land_area_km2':p['LAND_AREA_SQ_KM'],'boundary_year':2025})
    points=assign_regions(np.array([174.7633,174.7762,172.6362,170.5028]),np.array([-36.8485,-41.2865,-43.5321,-45.8788]),features)
    assert list(points)==['02','09','13','14'],list(points)
    records={};raw_count=0;rejected=[];duplicates=0;fields=None
    for path in sorted(raw.glob('geonet_*.csv')):
        with path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            if fields is None:fields=reader.fieldnames
            if reader.fieldnames!=fields:raise ValueError('GeoNet schema drift: '+path.name)
            for r in reader:
                raw_count+=1
                try:
                    date=datetime.datetime.fromisoformat(r['origintime'].replace('Z','+00:00'))
                    if r['eventtype']!='earthquake':raise ValueError('non-earthquake event')
                    if not 2020<=date.year<=2024:raise ValueError('outside half-open UTC study interval')
                    floats={k:float(r[k]) for k in ['longitude','latitude','magnitude','depth']}
                    if not all(math.isfinite(x) for x in floats.values()):raise ValueError('non-finite coordinate or measure')
                    if not (165<=floats['longitude']<=180 and -48<=floats['latitude']<=-33 and -3<=floats['magnitude']<=10 and -10<=floats['depth']<=800):raise ValueError('invalid core range')
                    if not r['publicid'] or not r['modificationtime']:raise ValueError('missing entity key or revision time')
                except (ValueError,KeyError) as err:
                    rejected.append({'public_id':r.get('publicid',''),'source_file':path.name,'reason':str(err)});continue
                r['source_file']=path.name
                if r['publicid'] in records:
                    duplicates+=1
                    if r['modificationtime']<=records[r['publicid']]['modificationtime']:continue
                records[r['publicid']]=r
    accepted=list(records.values());lon=np.array([float(r['longitude']) for r in accepted]);lat=np.array([float(r['latitude']) for r in accepted])
    codes=assign_regions(lon,lat,features)
    numeric={'usedphasecount','usedstationcount','magnitudestationcount','minimumdistance','azimuthalgap','originerror','magnitudeuncertainty'}
    renames={'publicid':'public_id','eventtype':'event_type','origintime':'origin_utc','modificationtime':'modified_utc','depth':'depth_km','magnitudetype':'magnitude_type','depthtype':'depth_type','evaluationmethod':'evaluation_method','evaluationstatus':'evaluation_status','evaluationmode':'evaluation_mode','earthmodel':'earth_model','usedphasecount':'used_phase_count','usedstationcount':'used_station_count','magnitudestationcount':'magnitude_station_count','minimumdistance':'minimum_distance','azimuthalgap':'azimuthal_gap','originerror':'origin_error','magnitudeuncertainty':'magnitude_uncertainty'}
    quakes=[]
    for r,code in zip(accepted,codes):
        q={}
        for k in fields:
            v=r[k].strip()
            if k in numeric|{'longitude','latitude','magnitude','depth'}:v=float(v) if v else None
            else:v=v or None
            q[renames.get(k,k)]=v
        q.update(event_year=int(r['origintime'][:4]),event_month=r['origintime'][:7],region_code=str(code),mapping_status='unmapped' if code=='00' else 'matched',source_file=r['source_file'])
        quakes.append(q)
    quakes.sort(key=lambda r:r['public_id'])
    book=openpyxl.load_workbook(raw/'statsnz_business_demography_2025.xlsx',read_only=True,data_only=True)
    sheet=list(book['Table 2'].values);years=list(sheet[6][2:12]);assert years==list(range(2016,2026))
    def norm(s):
        import unicodedata
        s=''.join(c for c in unicodedata.normalize('NFD',s) if not unicodedata.combining(c))
        return s.replace(' Region','').replace('region','').replace('-','').replace(' ','').lower()
    lookup={norm(r['region_name']):r['region_code'] for r in regions[1:]};lookup['areaoutside']='99'
    business=[];national=[]
    for i,row in enumerate(sheet):
        if row[1]!='Geographic units':continue
        name=str(row[0]).strip();nextrow=sheet[i+1];assert nextrow[1]=='Employee count'
        if name=='Manawatū-':name+='Whanganui'
        total=name.startswith('Total')
        if not total:
            key=norm(name);assert key in lookup,(name,key,lookup);code=lookup[key]
        for j,year in enumerate(years,2):
            units=row[j];employees=nextrow[j]
            if not isinstance(units,(int,float)) or not isinstance(employees,(int,float)):raise ValueError('Suppressed/non-numeric business cell')
            b={'reference_year':year,'geographic_units':int(units),'employees':int(employees)}
            if total:national.append(b)
            else:business.append({'region_code':code,**b,'source_sheet':'Table 2'})
    assert len(business)==170 and len(national)==10
    candidate=dbdir/'candidate.sqlite'
    if candidate.exists():candidate.unlink()
    con=sqlite3.connect(candidate);con.row_factory=sqlite3.Row;con.executescript(SCHEMA)
    with con:
        for table,rows in [('dim_region',regions),('fact_earthquake',quakes),('fact_business',business),('published_national_business',national)]:
            columns=list(rows[0]);sql=f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})'
            con.executemany(sql,[tuple(r[k] for k in columns) for r in rows])
        unmapped=int((codes=='00').sum())
        con.execute('INSERT INTO etl_audit VALUES (?,?,?,?,?,?,?,?,?)',(datetime.datetime.now(datetime.timezone.utc).isoformat(),datetime.datetime.now(datetime.timezone.utc).isoformat(),raw_count,len(quakes),duplicates,len(rejected),unmapped,time.perf_counter()-started,hashlib.sha256((raw/'source_manifest.json').read_bytes()).hexdigest()))
    assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not con.execute('PRAGMA foreign_key_check').fetchall()
    region_results=query_rows(con,'SELECT * FROM v_region_screening ORDER BY shallow_m4_events DESC, region_code')
    monthly=query_rows(con,'SELECT * FROM v_monthly_seismicity ORDER BY event_month,region_code')
    annual=query_rows(con,'SELECT event_year year,COUNT(*) event_count,SUM(magnitude>=4) m4_events,SUM(magnitude>=4 AND depth_km<=40) shallow_m4_events,MAX(magnitude) max_magnitude FROM fact_earthquake GROUP BY event_year')
    sensitivity=query_rows(con,'SELECT region_code,SUM(magnitude>=3 AND depth_km<=40) m3_shallow,SUM(magnitude>=4 AND depth_km<=40) m4_shallow,SUM(magnitude>=5 AND depth_km<=40) m5_shallow,SUM(magnitude>=4 AND depth_km<=70) m4_depth70 FROM fact_earthquake GROUP BY region_code')
    optional_missing={k:sum(r[k] is None for r in quakes) for k in quakes[0] if any(r[k] is None for r in quakes)}
    stats={'raw_records':raw_count,'accepted_records':len(quakes),'raw_columns':len(fields),'clean_columns':len(quakes[0]),'duplicates_removed':duplicates,'rejected_records':len(rejected),'unmapped_records':unmapped,'matched_records':len(quakes)-unmapped,'business_records':len(business),'region_records':len(regions),'optional_missing':optional_missing,'national_published_2025':national[-1],'regional_sum_2025':{'geographic_units':sum(b['geographic_units'] for b in business if b['reference_year']==2025),'employees':sum(b['employees'] for b in business if b['reference_year']==2025)},'annual':annual,'region_results':region_results,'sensitivity':sensitivity,'elapsed_seconds':round(time.perf_counter()-started,3),'integrity_check':'ok','foreign_key_check':[],'mapping_city_tests':list(points),'snapshot_utc':manifest['retrieved_utc']}
    for name,rows in [('earthquakes_clean',quakes),('regional_business_clean',business),('regions_clean',regions),('region_screening',region_results),('monthly_seismicity',monthly),('annual_seismicity',annual),('threshold_sensitivity',sensitivity)]:write_csv(clean/(name+'.csv'),rows)
    write_csv(clean/'rejected_records.csv',rejected,['public_id','source_file','reason'])
    write_csv(portfolio/'04_Data_Examples/earthquakes_raw_sample.csv',[{k:r[k] for k in fields} for r in accepted[:100]])
    write_csv(portfolio/'04_Data_Examples/earthquakes_clean_sample.csv',quakes[:100])
    (evidence/'analysis_results.json').write_text(json.dumps(stats,indent=2),encoding='utf-8')
    (dbdir/'schema.sql').write_text(SCHEMA,encoding='utf-8')
    with (dbdir/'restore.sql').open('w',encoding='utf-8') as f:
        for line in con.iterdump():f.write(line+'\n')
    con.close();os.replace(candidate,dbdir/'nz_resilience.sqlite')
    print(json.dumps({k:v for k,v in stats.items() if k not in ['region_results','sensitivity','optional_missing']},indent=2))
    print('TOP REGIONS',json.dumps(region_results[:7],indent=2))
    return stats

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2]);args=parser.parse_args();run(args.root)
