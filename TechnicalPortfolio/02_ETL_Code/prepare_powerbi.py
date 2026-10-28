"""Prepare local CSV paths and complete monthly periods for Power BI refresh."""
from pathlib import Path
import csv,re
ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT/'03_Dashboards'/'NZ_Business_Resilience_Project'
CLEAN=ROOT/'04_Data_Examples'/'Clean_Data'
def main():
 codes=[r['region_code'] for r in csv.DictReader((CLEAN/'regions_clean.csv').open(encoding='utf-8-sig'))]
 rows=list(csv.DictReader((CLEAN/'monthly_seismicity.csv').open(encoding='utf-8-sig')))
 lookup={(r['region_code'],r['event_month']):r for r in rows}
 monthly=PROJECT/'powerbi_monthly.csv'
 with monthly.open('w',newline='',encoding='utf-8') as f:
  w=csv.DictWriter(f,fieldnames=['region_code','event_month','event_count','m4_events','shallow_m4_events']);w.writeheader()
  for y in range(2020,2025):
   for m in range(1,13):
    period=f'{y}-{m:02}'
    for code in codes:w.writerow(lookup.get((code,period),dict(region_code=code,event_month=period,event_count=0,m4_events=0,shallow_m4_events=0)))
 sources={'Regions':CLEAN/'regions_clean.csv','Earthquakes':CLEAN/'earthquakes_clean.csv','Business':CLEAN/'regional_business_clean.csv','Monthly':monthly}
 for table,path in sources.items():
  p=PROJECT/'NZ_Business_Resilience.SemanticModel'/'definition'/'tables'/f'{table}.tmdl'
  t=p.read_text(encoding='utf-8');t,n=re.subn(r'File\.Contents\("[^"\n]+"\)',lambda _:f'File.Contents("{path.as_posix()}")',t)
  if n!=1:raise ValueError(f'Expected one CSV source in {table}, found {n}')
  p.write_text(t,encoding='utf-8')
 print('Prepared four local CSV sources and 1,080 monthly rows.')
 print('Open NZ_Business_Resilience.pbip, click Refresh, then save as PBIX.')
if __name__=='__main__':main()
