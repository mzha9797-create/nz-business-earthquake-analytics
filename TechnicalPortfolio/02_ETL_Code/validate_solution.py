"""Validate grain, source reconciliation, database constraints and SQL restoration."""
from pathlib import Path
import argparse,sqlite3,json,csv,collections,tempfile,hashlib

def run(root):
    root=Path(root);p=root/'TechnicalPortfolio';db=p/'01_Database/nz_resilience.sqlite'
    c=sqlite3.connect(db);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
    checks=[]
    def check(name,ok,detail=None):
        checks.append({'check':name,'passed':bool(ok),'detail':detail})
        if not ok:raise AssertionError(name+': '+str(detail))
    check('sqlite_integrity',c.execute('PRAGMA integrity_check').fetchone()[0]=='ok')
    check('foreign_keys',not c.execute('PRAGMA foreign_key_check').fetchall())
    check('unique_event_ids',c.execute('SELECT COUNT(*)=COUNT(DISTINCT public_id) FROM fact_earthquake').fetchone()[0]==1)
    check('business_grain',c.execute('SELECT COUNT(*) FROM fact_business').fetchone()[0]==170)
    counter=collections.Counter();types=collections.Counter();n=0;m4=0;shallow=0
    for f in (p/'04_Data_Examples/Raw_Data').glob('geonet_*.csv'):
        for r in csv.DictReader(f.open(encoding='utf-8-sig')):
            types[r['eventtype']]+=1
            if r['eventtype']!='earthquake' or not '2020-01-01'<=r['origintime']<'2025-01-01':continue
            n+=1;counter[r['origintime'][:4]]+=1;m4+=float(r['magnitude'])>=4;shallow+=float(r['magnitude'])>=4 and float(r['depth'])<=40
    expected=c.execute('SELECT COUNT(*),SUM(magnitude>=4),SUM(magnitude>=4 AND depth_km<=40) FROM fact_earthquake').fetchone()
    check('independent_raw_counts',(n,m4,shallow)==tuple(expected),{'raw':[n,m4,shallow],'sql':list(expected),'raw_event_types':dict(types)})
    check('regional_events_reconcile',c.execute('SELECT SUM(event_count) FROM v_region_screening').fetchone()[0]==n)
    check('regional_business_not_multiplied',c.execute('SELECT SUM(business_units_2025) FROM v_region_screening').fetchone()[0]==654465)
    check('source_periods',dict(counter)=={str(r['event_year']):r['n'] for r in c.execute('SELECT event_year,COUNT(*) n FROM fact_earthquake GROUP BY event_year')})
    before=c.execute('SELECT COUNT(*) FROM fact_business').fetchone()[0]
    for name,sql,args in [('unknown_foreign_key','INSERT INTO fact_business VALUES (?,?,?,?,?)',('xx',2025,1,1,'test')),('negative_business_count','UPDATE fact_business SET geographic_units=? WHERE region_code=? AND reference_year=?',(-1,'02',2025))]:
        c.execute('SAVEPOINT fault')
        blocked=False
        try:c.execute(sql,args)
        except sqlite3.IntegrityError:blocked=True
        finally:c.execute('ROLLBACK TO fault');c.execute('RELEASE fault')
        check(name+'_blocked',blocked)
    check('fault_injection_did_not_mutate_data',c.execute('SELECT COUNT(*) FROM fact_business').fetchone()[0]==before)
    with tempfile.TemporaryDirectory(prefix='nz_restore_') as t:
        restored=sqlite3.connect(Path(t)/'restored.sqlite')
        restored.executescript((p/'01_Database/restore.sql').read_text(encoding='utf-8'))
        check('restored_integrity',restored.execute('PRAGMA integrity_check').fetchone()[0]=='ok')
        for table in ['dim_region','fact_earthquake','fact_business','published_national_business']:
            a=c.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall();b=restored.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
            check('restore_exact_'+table,[tuple(r) for r in a]==b,{'row_count':len(a)})
        restored.close()
    # Dataset hashes protect downloaded evidence, not just aggregate results.
    meta=json.loads((p/'04_Data_Examples/Raw_Data/source_manifest.json').read_text())
    check('source_snapshots_unchanged',all(hashlib.sha256((p/'04_Data_Examples/Raw_Data'/s['file']).read_bytes()).hexdigest()==s['sha256'] for s in meta['sources']))
    result={'status':'PASS','checks':checks,'note':'Local restoration and independent arithmetic passed. Isolated environment testing is recorded separately.'}
    (p/'04_Data_Examples/Verification/validation_results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2));c.close()

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2]);run(a.parse_args().root)
