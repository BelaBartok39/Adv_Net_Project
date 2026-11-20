#!/usr/bin/env python3
"""Summarize recorded trials; failures stay in the dataset and missing costs stay blank."""
import argparse
import csv
import json
import sys
from pathlib import Path

# Support direct execution from the repository without requiring installation.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from advnet.metrics import inference_metrics
from advnet.scenarios import routing_evaluation

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory',type=Path,nargs='?',default=Path('runs'))
args=parser.parse_args()
fields=['trial_id','scenario','scenario_family','expected_route','mode','system_one_enabled','sysone_model','jev_threshold','catalog_sha256',
        'forced_escalation','classifier_route_correct','effective_route_correct','unnecessary_escalation','incorrect_catalog_choice','incorrect_no_change',
        'selected_route','effective_route','screening_error','baseline_reset_requested','baseline_reset_completed','baseline_source_sha256',
        'models','success','attempts','max_attempts','time_cap_seconds',
        'fault_to_acceptance_seconds','trial_elapsed_seconds','llm_calls','input_tokens','output_tokens',
        'jev_calls','jev_input_tokens','jev_output_tokens','jev_seconds','jev_cost_usd',
        'llm_input_tokens','llm_output_tokens','llm_seconds','llm_cost_usd','decision_seconds',
        'verify_seconds','deploy_seconds','convergence_seconds','measure_seconds','rollback_seconds',
        'cleanup_succeeded','cost_usd','error_type']
writer=csv.DictWriter(sys.stdout,fieldnames=fields)
writer.writeheader()
for path in sorted(args.directory.glob('*/result.json')):
    result=json.loads(path.read_text())
    row={k:result.get(k) for k in fields}
    calls=result.get('model_calls',[])
    row['models']=';'.join(sorted({c['model'] for c in calls if c.get('model')}))
    row.update(inference_metrics(calls))
    row.update(routing_evaluation(result))
    # Do not turn interrupted/unknown calls into a zero-dollar result.
    if result.get('error_type'):
        row['cost_usd']=None
    settings=result.get('decision_settings',{})
    row['sysone_model']=settings.get('sysone_model',settings.get('jev_model'))
    for key in ('jev_threshold','catalog_sha256'):
        row[key]=settings.get(key)
    groups={'decision_seconds':{'llm_decision','system_one_decision'},'verify_seconds':{'verify_candidate'},'deploy_seconds':{'deploy'},
            'convergence_seconds':{'fault_convergence','post_deploy_convergence'},
            'measure_seconds':{'measure_impaired','measure_candidate'},'rollback_seconds':{'rollback_candidate'}}
    for field,stages in groups.items():
        row[field]=sum(e['elapsed_seconds'] for e in result.get('events',[]) if e['stage'] in stages)
    writer.writerow(row)
