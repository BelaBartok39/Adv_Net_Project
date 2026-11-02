#!/usr/bin/env python3
"""Summarize recorded trials; failures stay in the dataset and missing costs stay blank."""
import argparse
import csv
import json
import sys
from pathlib import Path

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory',type=Path,nargs='?',default=Path('runs'))
args=parser.parse_args()
fields=['trial_id','scenario','mode','models','success','attempts','max_attempts','time_cap_seconds',
        'fault_to_acceptance_seconds','trial_elapsed_seconds','llm_calls','input_tokens','output_tokens',
        'llm_seconds','verify_seconds','deploy_seconds','convergence_seconds','measure_seconds','rollback_seconds',
        'cleanup_succeeded','cost_usd','error_type']
writer=csv.DictWriter(sys.stdout,fieldnames=fields)
writer.writeheader()
for path in sorted(args.directory.glob('*/result.json')):
    result=json.loads(path.read_text())
    row={k:result.get(k) for k in fields}
    calls=result.get('model_calls',[])
    row['models']=';'.join(sorted({c['model'] for c in calls if c.get('model')}))
    row['llm_calls']=len(calls)
    for key in ('input_tokens','output_tokens'):
        values=[c.get('usage',{}).get(key) for c in calls]
        row[key]=sum(values) if values and all(isinstance(v,int) for v in values) else None
    groups={'llm_seconds':{'llm_decision'},'verify_seconds':{'verify_candidate'},'deploy_seconds':{'deploy'},
            'convergence_seconds':{'fault_convergence','post_deploy_convergence'},
            'measure_seconds':{'measure_impaired','measure_candidate'},'rollback_seconds':{'rollback_candidate'}}
    for field,stages in groups.items():
        row[field]=sum(e['elapsed_seconds'] for e in result.get('events',[]) if e['stage'] in stages)
    writer.writerow(row)
