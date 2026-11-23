"""System One switch, matched inputs, accounting, and SDK contract checks."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from advnet.catalog import experiment_catalog, catalog_hash
from advnet.contracts import Observation
from advnet.experiment import main
from advnet.lab import Deadline
from advnet.metrics import inference_metrics
from advnet.pipeline import DecisionEngine
from advnet.providers import Jev, LLM, MockJev, MockLLM
from advnet.trial import DecisionProcess

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((ROOT/'topologies/abilene.json').read_text())


class SystemOneTests(unittest.TestCase):
    def test_default_catalog_covers_inventory_without_private_scenarios(self):
        catalog=experiment_catalog(MANIFEST)
        self.assertEqual(len(catalog),11)
        for node,router in MANIFEST['routers'].items():
            change=catalog['enable_host_'+node].proposal.changes[0]
            self.assertEqual((change.device,change.interface,change.value),(node,router['host_interface'],True))
        self.assertNotIn('sea-host-interface-down',str(catalog))

    def test_both_modes_pass_same_catalog_to_subprocess_and_record_distinct_mode(self):
        obs=Observation.model_validate_json((ROOT/'examples/congestion.json').read_text())
        catalog=experiment_catalog(MANIFEST)
        for mode,forced in (('off',False),('jev',False),('jev',True)):
            with self.subTest(mode=mode,forced=forced), tempfile.TemporaryDirectory() as temporary:
                process=DecisionProcess('compatible','pinned-model',temporary,'https://example.test/v1',
                                        system_one=mode,sysone_model='typesafe/jev-test',catalog=catalog,force_escalation=forced)
                response=SimpleNamespace(returncode=0,stdout='{"status":"decision_complete"}',stderr='')
                with patch('advnet.trial.subprocess.run',return_value=response) as run:
                    process.decide(obs,[],1,Deadline(30))
                argv=run.call_args.args[0]
                self.assertEqual('--without-jev' in argv,mode=='off')
                self.assertEqual('--sysone-model' in argv,mode=='jev')
                self.assertEqual('--force-escalation' in argv,forced)
                self.assertEqual(process.settings['forced_escalation'],forced)
                self.assertEqual(argv[argv.index('--llm-model')+1],'pinned-model')
                if mode=='jev':
                    self.assertEqual(argv[argv.index('--sysone-model')+1],'typesafe/jev-test')
                self.assertEqual(process.settings['catalog_sha256'],catalog_hash(catalog))
                actual=json.loads((Path(temporary)/'catalog.json').read_text())
                self.assertEqual(set(actual),set(catalog))

    def test_missing_system_one_model_blocks_before_lab_access_or_fault(self):
        args=['experiment','run','--scenario','scenarios/sea-host-interface-down.json',
              '--model','test-model','--system-one','jev']
        with patch.dict(os.environ,{'OPEN_ROUTER_KEY':'test-only'},clear=True), \
             patch.object(sys,'argv',args), patch('advnet.experiment.Lab') as lab, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code,2)
        lab.assert_not_called()

    def test_missing_shared_key_blocks_before_lab_access(self):
        args=['experiment','run','--scenario','scenarios/sea-host-interface-down.json',
              '--model','test-model','--system-one','off']
        with patch.dict(os.environ,{},clear=True), patch.object(sys,'argv',args), \
             patch('advnet.experiment.Lab') as lab, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main()
        lab.assert_not_called()

    def test_llm_only_needs_no_system_one_model_or_separate_key(self):
        args=['experiment','run','--scenario','scenarios/sea-host-interface-down.json','--model','test-llm']
        with patch.dict(os.environ,{'OPEN_ROUTER_KEY':'test-only'},clear=True), \
             patch.object(sys,'argv',args), patch('advnet.experiment.Lab',side_effect=RuntimeError('preflight passed')):
            with self.assertRaisesRegex(RuntimeError,'preflight passed'):
                main()

    def test_standalone_reads_distinct_llm_and_system_one_environment_models(self):
        from advnet.__main__ import main as decide_main
        with tempfile.TemporaryDirectory() as temporary:
            args=['advnet',str(ROOT/'examples/congestion.json'),'--mode','live',
                  '--catalog',str(ROOT/'examples/actions.json'),'--log',str(Path(temporary)/'decisions.jsonl')]
            env={'OPEN_ROUTER_KEY':'test-only','LLM_MODEL':'nvidia/test-llm','SYSONE_MODEL':'typesafe/test-decision'}
            with patch.dict(os.environ,env,clear=True), patch.object(sys,'argv',args), \
                 patch('advnet.__main__.Jev',return_value=MockJev('avoid_r2_path')) as jev, \
                 patch('advnet.__main__.LLM') as llm, redirect_stdout(io.StringIO()):
                self.assertEqual(decide_main(),0)
            jev.assert_called_once_with('typesafe/test-decision',10)
            self.assertEqual(llm.call_args.args[:2],('openrouter','nvidia/test-llm'))

    def test_unknown_manifest_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'wrong.json'
            path.write_text((ROOT/'examples/actions.json').read_text())
            with self.assertRaises(ValueError):
                experiment_catalog(MANIFEST,path)

    def test_usage_keeps_jev_separate_and_supports_openrouter_token_names(self):
        metrics=inference_metrics([
            {'stage':'jev','elapsed_ms':12,'usage':{'input_tokens':100,'output_tokens':0}},
            {'stage':'llm','elapsed_ms':2000,'usage':{'prompt_tokens':300,'completion_tokens':20,'cost':0}},
        ])
        self.assertEqual(metrics['jev_calls'],1)
        self.assertEqual(metrics['llm_calls'],1)
        self.assertEqual(metrics['input_tokens'],400)
        self.assertEqual(metrics['llm_input_tokens'],300)
        self.assertEqual(metrics['jev_seconds'],0.012)
        self.assertIsNone(metrics['cost_usd'])
        self.assertEqual(metrics['llm_cost_usd'],0)
        shortcut=inference_metrics([{'stage':'jev','elapsed_ms':5,'usage':{}}])
        self.assertEqual(shortcut['llm_calls'],0)
        self.assertIsNone(shortcut['jev_input_tokens'])

    def test_summary_csv_preserves_stage_counts_and_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'trial'; root.mkdir()
            (root/'result.json').write_text(json.dumps({'trial_id':'test','mode':'jev_llm','success':False,
                'system_one_enabled':True,'model_calls':[{'stage':'jev','elapsed_ms':5,'usage':{'input_tokens':7,'output_tokens':0}}]}))
            output=subprocess.run([sys.executable,str(ROOT/'scripts/summarize-trials.py'),temporary],
                                  capture_output=True,text=True,check=True)
            import csv
            row=next(csv.DictReader(io.StringIO(output.stdout)))
            self.assertEqual(row['llm_calls'],'0')
            self.assertEqual(row['jev_calls'],'1')
            self.assertEqual(row['success'],'False')
            self.assertEqual(row['cost_usd'],'')

    @unittest.skipUnless(importlib.util.find_spec('typesafe_sdk'),'Optional TypeSafe SDK not installed')
    def test_official_sdk_adapter_with_mock_http_transport(self):
        import httpx2
        from typesafe_sdk import TypeSafeClient
        seen=[]
        def handle(request):
            self.assertEqual(str(request.url),'https://openrouter.ai/api/v1/systemone')
            self.assertEqual(request.headers['authorization'],'Bearer test-openrouter-key')
            seen.append(json.loads(request.content))
            return httpx2.Response(200,json={'model':'jev-test-resolved','usage':{'input_tokens':12,'output_tokens':0,'cost':0.00003},
                'answers':{'route':{'type':'choice','choice':'escalate','confidence':0.95,
                                    'probabilities':{'escalate':0.95,'no_change':0.05}}}})
        def client(**kwargs):
            self.assertEqual(kwargs['retry'].max_retries,0)
            return TypeSafeClient(transport=httpx2.MockTransport(handle),**kwargs)
        with patch.dict(os.environ,{'OPEN_ROUTER_KEY':'test-openrouter-key'},clear=True), \
             patch('typesafe_sdk.TypeSafeClient',side_effect=client):
            reply=Jev('jev-pinned',10).classify({'telemetry':'test-only'},
                    {'escalate':'Needs complex reasoning','no_change':'Healthy'})
        self.assertEqual(reply.model,'jev-test-resolved')
        self.assertEqual(reply.value['choice'],'escalate')
        self.assertEqual(reply.usage['input_tokens'],12)
        self.assertEqual(reply.usage['cost'],0.00003)
        self.assertEqual(seen[0]['model'],'jev-pinned')
        self.assertEqual(set(seen[0]['questions']['route']['criteria']),{'escalate','no_change'})

    @unittest.skipUnless(importlib.util.find_spec('openai'),'Optional OpenAI-compatible SDK not installed')
    def test_llm_explicitly_uses_shared_key_and_openrouter_chat_endpoint(self):
        import httpx
        from openai import OpenAI
        seen=[]
        def handle(request):
            self.assertEqual(str(request.url),'https://openrouter.ai/api/v1/chat/completions')
            self.assertEqual(request.headers['authorization'],'Bearer test-openrouter-key')
            seen.append(json.loads(request.content))
            return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,
                'model':'nemotron-test-resolved','choices':[{'index':0,'finish_reason':'stop',
                'message':{'role':'assistant','content':'{}'}}],
                'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12,'cost':0}})
        def client(**kwargs):
            return OpenAI(http_client=httpx.Client(transport=httpx.MockTransport(handle)),**kwargs)
        with patch.dict(os.environ,{'OPEN_ROUTER_KEY':'test-openrouter-key'},clear=True), \
             patch('openai.OpenAI',side_effect=client):
            reply=LLM('openrouter','nvidia/test-llm',10,100).propose('test system',{'state':'test'})
        self.assertEqual(seen[0]['model'],'nvidia/test-llm')
        self.assertEqual(reply.model,'nemotron-test-resolved')
        self.assertEqual(reply.usage['cost'],0)


if __name__=='__main__':
    unittest.main()
