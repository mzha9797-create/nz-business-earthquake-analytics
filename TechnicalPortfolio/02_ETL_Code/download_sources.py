"""Download the public snapshot sources identified by the original manifest."""
from pathlib import Path
import argparse,json,hashlib,urllib.request
root=Path(__file__).resolve().parents[1]
raw=root/'04_Data_Examples/Raw_Data'
parser=argparse.ArgumentParser();parser.add_argument('--strict-hashes',action='store_true');args=parser.parse_args()
manifest=json.loads((raw/'source_manifest.json').read_text())
for source in manifest['sources']:
 target=raw/source['file']
 if not target.exists():
  print('Downloading',source['file'])
  with urllib.request.urlopen(source['url'],timeout=180) as response:target.write_bytes(response.read())
 digest=hashlib.sha256(target.read_bytes()).hexdigest()
 if digest!=source['sha256']:
  print('Source differs from the original snapshot:',source['file'])
  if args.strict_hashes:raise ValueError('Exact historical snapshot not reproduced')
print('Download complete. Review any source drift before rebuilding the warehouse.')
