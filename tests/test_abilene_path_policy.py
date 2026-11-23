"""Abilene path experiment: offline fixtures and opt-in real Batfish verification."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from advnet.contracts import DeviceState, Observation
from advnet.eos import candidate_configs
from advnet.lab import Deadline
from advnet.trial import ProcessVerifier, Trial, dump
from scripts.build_abilene_path_experiment import ROOT, build_experiment, experiment_inputs


class MemoryAbileneLab:
    """Synthetic measurements and dictionary writes only; no live transport."""
    def __init__(self):
        _, self.manifest, self.original, _, _ = experiment_inputs()
        self.state = dict(self.original)
        self.writes = []
        self.shapes = []
        self.capacities = {}
        self.measurements = []

    def configs(self, deadline=None):
        return dict(self.state)

    def wait_convergence(self, deadline):
        deadline.remaining()

    def shape(self, capacities, deadline):
        deadline.remaining()
        if capacities not in ({}, {'den-kcy': 2}):
            raise AssertionError('Unexpected simulated capacity change')
        self.capacities = dict(capacities)
        self.shapes.append(dict(capacities))

    def replace(self, configs, directory, deadline):
        # Reject any attempt to deploy a candidate, even if another assertion fails.
        if Path(directory).name != 'reset' or configs != self.original:
            raise AssertionError('Only simulated restoration of baseline is permitted')
        self.writes.append({'stage': 'reset', 'devices': sorted(configs)})
        self.state.update(configs)

    def measure(self, deadline, flow=None):
        deadline.remaining()
        congested = bool(self.capacities)
        hosts = [r['host'] for r in self.manifest['routers'].values()]
        value = {'synthetic': True, 'all_reachable': True, 'ospf_healthy': True,
                 'mean_rtt_ms': 4.0, 'ospf_neighbors': {},
                 'pairs': [{'source': a, 'destination': b, 'reachable': True,
                            'loss_fraction': 0.0} for a in hosts for b in hosts if a != b],
                 'traffic': ({'synthetic': True, 'received_mbps': 2 if congested else 5,
                              'loss_fraction': 0.6 if congested else 0.0,
                              'max_link_utilization': 2.5 if congested else 0.05}
                             if flow else None)}
        self.measurements.append(value)
        return value

    def observe(self, identifier, measurements, deadline, objective):
        deadline.remaining()
        return Observation(
            scenario_id=identifier, observed_at=datetime.now(timezone.utc), objective=objective,
            requirements=self.manifest['requirements'], measurements=measurements,
            topology=[f"{l['a']}:{l['a_interface']} -- {l['b']}:{l['b_interface']}"
                      for l in self.manifest['links']],
            devices={n: DeviceState(interfaces=self.manifest['routers'][n]['interfaces'],
                                    running_config=text) for n, text in self.state.items()})


class FixedCostDecision:
    def __init__(self, directory):
        self.directory = directory
        _, _, _, self.proposal, _ = experiment_inputs()
        self.feedback_seen = []

    def decide(self, observation, feedback, attempt, deadline):
        recorded = [f.model_dump(mode='json') for f in feedback]
        self.feedback_seen.append(recorded)
        dump(self.directory / f'attempt-{attempt}/feedback.json', recorded)
        dump(self.directory / f'attempt-{attempt}/observation.json', observation.model_dump(mode='json'))
        return {'status': 'decision_complete', 'decision_id': f'fixed-cost-{attempt}',
                'proposal': self.proposal.model_dump(mode='json'), 'calls': []}


def assert_controller_rejected(test, directory, lab, decision, result):
    test.assertNotIn('error_type', result, result)
    test.assertFalse(result['success'])  # Expected: the unsafe optimization was rejected.
    test.assertTrue(result['cleanup_succeeded'], result)
    test.assertEqual(result['model_calls'], [])
    stages = [e['stage'] for e in result['events']]
    test.assertEqual(stages.count('verify_candidate'), 1)
    for stage in ('deploy', 'rollback_candidate', 'measure_candidate'):
        test.assertNotIn(stage, stages)
    test.assertEqual([w['stage'] for w in lab.writes], ['reset'])
    test.assertEqual(lab.shapes, [{}, {'den-kcy': 2}, {}])
    test.assertEqual(lab.state, lab.original)
    test.assertEqual(lab.capacities, {})
    feedback = decision.feedback_seen[1][0]
    test.assertEqual(feedback['stage'], 'verification')
    test.assertIs(feedback['accepted'], False)
    candidate = json.loads((directory / 'attempt-1/verification.json').read_text())
    test.assertEqual(feedback['details'], candidate['details'])
    test.assertTrue(all('violates path_policies[0]' in d for d in candidate['details']), candidate)
    test.assertTrue(lab.measurements[1]['all_reachable'])
    test.assertTrue(lab.measurements[1]['ospf_healthy'])
    test.assertGreater(lab.measurements[1]['traffic']['max_link_utilization'],
                       lab.measurements[0]['traffic']['max_link_utilization'] * 1.05)
    return candidate


class AbilenePathExperimentTests(unittest.TestCase):
    def test_separate_manifest_candidate_and_reproducible_snapshot_build(self):
        baseline, manifest, configs, proposal, _ = experiment_inputs()
        self.assertNotIn('path_policies', baseline)
        self.assertEqual(len(manifest['routers']), 11)
        self.assertEqual(len(manifest['path_policies']), 2)
        for policy in manifest['path_policies']:
            self.assertEqual(policy['required_transit_routers'], [])
            self.assertEqual(policy['forbidden_transit_routers'], ['lax'])
        candidate = candidate_configs(configs, proposal)
        self.assertEqual({n for n in configs if configs[n] != candidate[n]}, {'den'})
        self.assertEqual(candidate['den'], configs['den'].replace(
            '   ip ospf cost 10\n   no shutdown\n!\ninterface Ethernet4',
            '   no shutdown\n   ip ospf cost 100\n!\ninterface Ethernet4'))
        with tempfile.TemporaryDirectory() as tmp:
            one = build_experiment(Path(tmp) / 'one')
            two = build_experiment(Path(tmp) / 'two')
            files = lambda root: {str(p.relative_to(root)): p.read_bytes()
                                  for p in root.rglob('*') if p.is_file()}
            self.assertEqual(files(one), files(two))
            with self.assertRaises(FileExistsError):
                build_experiment(one)
            for name in ('healthy', 'candidate', 'candidate-no-policy'):
                directory = one / name / 'snapshot'
                self.assertEqual(len(list((directory / 'configs').glob('*.cfg'))), 11)
                self.assertEqual(len(list((directory / 'hosts').glob('*.json'))), 11)
                expected = baseline if name == 'candidate-no-policy' else manifest
                self.assertEqual(json.loads((directory / 'intent.json').read_text()), expected)
        self.assertEqual(json.loads((ROOT / 'topologies/abilene.json').read_text()), baseline)
        for node, text in configs.items():
            self.assertEqual((ROOT / f'labs/configs/abilene/{node}.cfg').read_text(), text)

    def test_actual_trial_preserves_policy_and_skips_deployment_on_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'trial'
            lab = MemoryAbileneLab()
            decision = FixedCostDecision(directory)
            _, _, _, _, scenario = experiment_inputs()
            rejected = {'accepted': False, 'details': ['h-sea -> h-nyc: violates path_policies[0] (offline fixture)']}
            with patch.object(ProcessVerifier, 'verify', side_effect=[
                    {'accepted': True, 'details': ['offline healthy fixture']}, rejected]):
                result = Trial(lab, decision, ProcessVerifier(['unused']), directory,
                               max_attempts=2).run(scenario)
            assert_controller_rejected(self, directory, lab, decision, result)
            intent = json.loads((directory / 'attempt-1/snapshot/intent.json').read_text())
            self.assertEqual(intent, lab.manifest)
            self.assertEqual((directory / 'attempt-1/snapshot/configs/den.cfg').read_text(),
                             '! device: den (cEOSLab, EOS-4.36.3M)\n'
                             + candidate_configs(lab.original, decision.proposal)['den'])


@unittest.skipUnless(os.environ.get('RUN_BATFISH_INTEGRATION') == '1',
                     'Set RUN_BATFISH_INTEGRATION=1 to use real Batfish')
class LiveAbilenePathExperimentTests(unittest.TestCase):
    def test_real_path_rejection_and_actual_controller_with_simulated_routers(self):
        from pybatfish.client.session import Session
        from pybatfish.datamodel.flow import HeaderConstraints

        output = Path(os.environ.get('ADVNET_TEST_OUTPUT_DIR', str(ROOT / 'runs')))
        directory = output / ('abilene-path-policy-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
                              + uuid4().hex[:8])
        build_experiment(directory)
        protected = [ROOT / 'topologies/abilene.json', ROOT / 'labs/abilene.clab.yml',
                     ROOT / 'advnet/batfish_gate.py', ROOT / 'advnet/trial.py',
                     ROOT / 'advnet/eos.py', ROOT / 'advnet/lab.py',
                     *sorted((ROOT / 'labs/configs/abilene').glob('*.cfg')),
                     *sorted((ROOT / 'labs/configs/diamond').glob('*.cfg'))]
        digests = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        bf = Session(host=os.environ.get('BATFISH_HOST', 'localhost'))
        report = {'server': os.environ.get('BATFISH_HOST', 'localhost') + ':9996',
                  'integration_passed': False, 'cases': {}, 'protected_sha256_before': digests,
                  'actual': ['Batfish gate subprocesses, parsing, nodes and all forwarding traces',
                             'Trial, ProcessVerifier and typed candidate generation'],
                  'simulated': ['router operations', 'capacity shaping', 'runtime measurements'],
                  'decision_source': 'predetermined typed Proposal; no LLM'}
        commands = []
        gate_command = [sys.executable, '-m', 'advnet.batfish_gate']
        run_process = subprocess.run

        def gate_only(args, **kwargs):
            if args[:3] != gate_command or len(args) != 4:
                raise AssertionError(f'Forbidden subprocess: {args}')
            commands.append(args)
            return run_process(args, **kwargs)

        def inspect(name, candidate, gate, identifier):
            bf.set_snapshot(identifier)
            manifest = json.loads((candidate / 'intent.json').read_text())
            parsed = bf.q.fileParseStatus().answer().frame()
            issues = bf.q.initIssues().answer().frame()
            nodes = bf.q.nodeProperties().answer().frame()
            evidence = {'gate': gate, 'snapshot': identifier, 'parse_results': parsed.to_dict('records'),
                        'initialization_issues': issues.to_dict('records'),
                        'modeled_nodes': sorted(str(n) for n in nodes['Node']), 'flows': [],
                        'snapshot_directory': str(candidate.resolve()),
                        'input_sha256': {str(p.relative_to(candidate)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in sorted(candidate.rglob('*')) if p.is_file()}}
            report['cases'][name] = evidence
            routers = manifest['routers']
            hosts = {r['host']: r['host_ip'] for r in routers.values()}
            expected = {**{f'configs/{r}.cfg': 'ARISTA' for r in routers},
                        **{f'hosts/{h}.json': 'HOST' for h in hosts}}
            self.assertEqual(len(parsed), 22)
            self.assertEqual(set(parsed['File_Name']), set(expected))
            for row in parsed.itertuples():
                self.assertEqual(row.Status, 'PASSED')
                self.assertEqual(row.File_Format, expected[row.File_Name])
            self.assertTrue(issues.empty, evidence)
            self.assertEqual(set(nodes['Node']), set(routers) | set(hosts))
            for destination, ip in hosts.items():
                sources = set(hosts) - {destination}
                frame = bf.q.traceroute(
                    startLocation=','.join(json.dumps(n) for n in sorted(sources)),
                    headers=HeaderConstraints(dstIps=ip, ipProtocols=['ICMP']),
                    maxTraces=10000, ignoreFilters=False).answer().frame()
                self.assertEqual(len(frame), 10)
                self.assertEqual({r.Flow.ingressNode for r in frame.itertuples()}, sources)
                for row in frame.itertuples():
                    self.assertEqual(str(row.Flow.srcIp), hosts[row.Flow.ingressNode])
                    self.assertEqual(str(row.Flow.dstIp), ip)
                    self.assertEqual(str(row.Flow.ipProtocol).upper(), 'ICMP')
                    self.assertGreater(row.TraceCount, 0)
                    self.assertEqual(row.TraceCount, len(row.Traces))
                    paths = []
                    for trace in row.Traces:
                        self.assertEqual(str(trace.disposition), 'ACCEPTED')
                        path = [h.node for h in trace.hops]
                        self.assertEqual(path[0], row.Flow.ingressNode)
                        self.assertEqual(path[-1], destination)
                        paths.append({'disposition': str(trace.disposition), 'path': path})
                    evidence['flows'].append({'source': row.Flow.ingressNode, 'destination': destination,
                                              'trace_count': int(row.TraceCount), 'traces': paths})
            self.assertEqual(len(evidence['flows']), 110)
            return evidence

        try:
            bf.set_network('advnet-abilene')
            verifier = ProcessVerifier(gate_command)
            with patch('advnet.trial.subprocess.run', side_effect=gate_only):
                for name in ('healthy', 'candidate', 'candidate-no-policy'):
                    candidate = directory / name / 'snapshot'
                    before = set(bf.list_snapshots())
                    gate = verifier.verify(candidate, Deadline(300))
                    created = set(bf.list_snapshots()) - before
                    self.assertEqual(len(created), 1, gate)
                    evidence = inspect(name, candidate, gate, created.pop())
                    self.assertIs(gate['accepted'], name != 'candidate', gate)
                    protected_flow = next(f for f in evidence['flows']
                                          if (f['source'], f['destination']) == ('h-sea', 'h-nyc'))
                    expected = (['h-sea', 'sea', 'den', 'kcy', 'ind', 'chi', 'nyc', 'h-nyc']
                                if name == 'healthy' else
                                ['h-sea', 'sea', 'sva', 'lax', 'hou', 'atl', 'was', 'nyc', 'h-nyc'])
                    self.assertTrue(all(t['path'] == expected for t in protected_flow['traces']))
                    if name == 'candidate':
                        self.assertTrue(all('violates path_policies[0]' in d for d in gate['details']), gate)
                        self.assertTrue(any(c['forbidden_routers'] == ['lax'] for c in gate['path_checks']))

                lab = MemoryAbileneLab()
                trial_directory = directory / 'trial'
                decision = FixedCostDecision(trial_directory)
                _, _, _, _, scenario = experiment_inputs()
                before = set(bf.list_snapshots())
                result = Trial(lab, decision, verifier, trial_directory, max_attempts=2).run(scenario)
                report.update(trial_result=result, simulated_writes=lab.writes, simulated_shapes=lab.shapes,
                              simulated_measurements=lab.measurements, feedback_seen=decision.feedback_seen,
                              subprocesses=commands)
                candidate_gate = assert_controller_rejected(self, trial_directory, lab, decision, result)
                healthy_gate = json.loads((trial_directory / 'healthy-verification.json').read_text())
                self.assertTrue(healthy_gate['accepted'], healthy_gate)
                created = set(bf.list_snapshots()) - before
                self.assertEqual(len(created), 2)
                for identifier in created:
                    bf.set_snapshot(identifier)
                    uploaded = bf.get_snapshot_input_object_text('configs/den.cfg')
                    is_candidate = '   ip ospf cost 100\n' in uploaded
                    relative = 'attempt-1/snapshot' if is_candidate else 'healthy-snapshot'
                    candidate = trial_directory / relative
                    self.assertEqual(uploaded, (candidate / 'configs/den.cfg').read_text())
                    gate = candidate_gate if is_candidate else healthy_gate
                    inspect('trial-candidate' if is_candidate else 'trial-healthy', candidate, gate, identifier)
                self.assertEqual(set(report['cases']), {'healthy', 'candidate', 'candidate-no-policy',
                                                       'trial-healthy', 'trial-candidate'})
                self.assertEqual(len(commands), 5)
                self.assertEqual((trial_directory / 'attempt-1/snapshot/configs/den.cfg').read_bytes(),
                                 (directory / 'candidate/snapshot/configs/den.cfg').read_bytes())
            report['protected_files_unchanged'] = all(
                hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in digests.items())
            self.assertTrue(report['protected_files_unchanged'])
            report['integration_passed'] = True
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            for name, evidence in report['cases'].items():
                target = directory / name
                target.mkdir(exist_ok=True)
                dump(target / 'evidence.json', evidence)
            dump(directory / 'evidence.json', report)
            print(f'\nAbilene path-policy evidence: {directory / "evidence.json"}', flush=True)


if __name__ == '__main__':
    unittest.main()
