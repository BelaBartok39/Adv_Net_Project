"""Controller tests with simulated routers; opt in to real localhost Batfish checks."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from advnet.contracts import DeviceState, Observation, Proposal
from advnet.lab import Deadline
from advnet.trial import ProcessVerifier, Trial, dump, snapshot

ROOT = Path(__file__).resolve().parents[1]


def disable_proposal(interface):
    return Proposal(outcome='candidate', diagnosis='Predetermined test-only interface shutdown.',
                    evidence=['Controlled integration fixture, not an LLM response.'],
                    changes=[{'device': 'r1', 'interface': interface,
                              'action': 'set_interface_enabled', 'value': False}],
                    expected_effect='Intentionally impair the modeled Diamond network.')


def repair_scenario():
    # A backbone fault leaves the alternate route usable but breaks an expected
    # OSPF adjacency. Trial's repair precondition therefore holds before deciding.
    return {'name': 'in-memory-diamond-bad-candidate', 'objective': 'repair',
            'objective_text': 'Restore all Diamond host reachability and OSPF adjacencies.',
            'fault': disable_proposal('Ethernet1').model_dump(mode='json')}


class MemoryDiamondLab:
    """Only Python dictionaries change. This class has no Docker/CLI transport."""
    def __init__(self):
        self.original = {node: (ROOT / f'labs/configs/diamond/{node}.cfg').read_text()
                         for node in ('r1', 'r2', 'r3', 'r4')}
        self.state = dict(self.original)
        self.writes = []
        self.measurements = []
        self.manifest = {
            'lab': 'advnet-diamond',
            'routers': {node: {
                'interfaces': re.findall(r'^interface (Ethernet\S+)$', text, re.M),
                'router_id': re.search(r'^   router-id (\S+)$', text, re.M)[1],
            } for node, text in self.original.items()},
            'hosts': {
                'host-a': {'host_ip': '10.10.1.10', 'gateway': '10.10.1.1',
                           'router': 'r1', 'router_interface': 'Ethernet3'},
                'host-b': {'host_ip': '10.10.2.10', 'gateway': '10.10.2.1',
                           'router': 'r4', 'router_interface': 'Ethernet3'},
            },
            'links': [
                {'a': 'r1', 'a_interface': 'Ethernet1', 'b': 'r2', 'b_interface': 'Ethernet1'},
                {'a': 'r1', 'a_interface': 'Ethernet2', 'b': 'r3', 'b_interface': 'Ethernet1'},
                {'a': 'r2', 'a_interface': 'Ethernet2', 'b': 'r4', 'b_interface': 'Ethernet1'},
                {'a': 'r3', 'a_interface': 'Ethernet2', 'b': 'r4', 'b_interface': 'Ethernet2'},
            ],
            'requirements': ['Both test hosts must reach each other over the data network.',
                             'All four backbone OSPF adjacencies must be healthy.'],
        }

    def configs(self, deadline=None):
        return dict(self.state)

    def enabled(self, node, interface):
        block = re.search(rf'(?m)^interface {re.escape(interface)}\n(?:[ \t]+.*\n)*', self.state[node])
        if block is None:
            raise ValueError(f'Missing fixture interface {node}/{interface}')
        return not any(line.strip() == 'shutdown' for line in block[0].splitlines())

    def wait_convergence(self, deadline):
        deadline.remaining()  # No sleep or live polling: measurements are synthetic.

    def measure(self, deadline=None, flow=None):
        if flow:
            raise AssertionError('This fixture does not run traffic experiments')
        links = self.manifest['links']
        up = [self.enabled(l['a'], l['a_interface']) and self.enabled(l['b'], l['b_interface'])
              for l in links]
        reachable = (self.enabled('r1', 'Ethernet3') and self.enabled('r4', 'Ethernet3')
                     and ((up[0] and up[2]) or (up[1] and up[3])))
        neighbors = {node: [] for node in self.state}
        for link, active in zip(links, up):
            if active:
                for a, b in (('a', 'b'), ('b', 'a')):
                    neighbors[link[a]].append(
                        f"{self.manifest['routers'][link[b]]['router_id']} 1 default 0 FULL")
        value = {'synthetic': True, 'all_reachable': reachable, 'ospf_healthy': all(up),
                 'mean_rtt_ms': 4.0 if reachable else None, 'traffic': None,
                 'ospf_neighbors': {n: '\n'.join(rows) for n, rows in neighbors.items()},
                 'pairs': [{'source': a, 'destination': b, 'reachable': reachable,
                            'loss_fraction': 0.0 if reachable else 1.0}
                           for a, b in (('host-a', 'host-b'), ('host-b', 'host-a'))]}
        self.measurements.append(value)
        return value

    def observe(self, identifier, measurements, deadline, objective):
        topology = [f"{l['a']}:{l['a_interface']} -- {l['b']}:{l['b_interface']}"
                    for l in self.manifest['links']]
        topology += [f"{h['router']}:{h['router_interface']} -- {name}:eth1"
                     for name, h in self.manifest['hosts'].items()]
        return Observation(scenario_id=identifier, observed_at=datetime.now(timezone.utc),
                           objective=objective, requirements=self.manifest['requirements'],
                           topology=topology, measurements=measurements,
                           devices={n: DeviceState(interfaces=self.manifest['routers'][n]['interfaces'],
                                                   running_config=text)
                                    for n, text in self.state.items()})

    def replace(self, configs, directory, deadline=None):
        stage = Path(directory).name
        if stage not in {'injection', 'deploy', 'rollback', 'reset'} or not set(configs) <= set(self.state):
            raise AssertionError('Unexpected simulated configuration write')
        self.writes.append({'stage': stage, 'devices': sorted(configs),
                            'sha256': {n: hashlib.sha256(t.encode()).hexdigest() for n, t in configs.items()}})
        self.state.update(configs)

    def shape(self, *args):
        raise AssertionError('Traffic shaping must not run in this fixture')


class FixedDecision:
    def __init__(self, directory):
        self.directory = directory
        self.proposal = disable_proposal('Ethernet3')
        self.feedback_seen = []

    def decide(self, observation, feedback, attempt, deadline):
        recorded = [f.model_dump(mode='json') for f in feedback]
        self.feedback_seen.append(recorded)
        # Match the normal decision adapter's feedback artifact without any LLM.
        dump(self.directory / f'attempt-{attempt}' / 'feedback.json', recorded)
        return {'status': 'decision_complete', 'decision_id': f'fixed-{attempt}',
                'proposal': self.proposal.model_dump(mode='json'), 'calls': []}


class SnapshotCompatibilityTests(unittest.TestCase):
    def test_trial_snapshot_models_diamond_separate_hosts(self):
        lab = MemoryDiamondLab()
        with tempfile.TemporaryDirectory() as tmp:
            directory = snapshot(Path(tmp), lab.configs(), lab.manifest)
            self.assertEqual({p.stem for p in (directory / 'configs').iterdir()}, set(lab.state))
            self.assertEqual({p.stem for p in (directory / 'hosts').iterdir()}, {'host-a', 'host-b'})
            for name, host in lab.manifest['hosts'].items():
                self.assertEqual(json.loads((directory / f'hosts/{name}.json').read_text()), {
                    'hostname': name, 'hostInterfaces': {'eth1': {
                        'name': 'eth1', 'prefix': host['host_ip'] + '/24', 'gateway': host['gateway']}}})
            self.assertEqual(json.loads((directory / 'intent.json').read_text()), lab.manifest)

    def test_trial_snapshot_preserves_original_abilene_structure(self):
        manifest = json.loads((ROOT / 'topologies/abilene.json').read_text())
        configs = {n: (ROOT / f'labs/configs/abilene/{n}.cfg').read_text() for n in manifest['routers']}
        with tempfile.TemporaryDirectory() as tmp:
            directory = snapshot(Path(tmp), configs, manifest)
            self.assertEqual(len(list((directory / 'configs').iterdir())), 11)
            self.assertEqual(len(list((directory / 'hosts').iterdir())), 11)
            for node, text in configs.items():
                self.assertEqual((directory / f'configs/{node}.cfg').read_text(),
                                 f'! device: {node} (cEOSLab, EOS-4.36.3M)\n' + text)
            for host in manifest['routers'].values():
                self.assertEqual(json.loads((directory / f'hosts/{host["host"]}.json').read_text()), {
                    'hostname': host['host'], 'hostInterfaces': {'eth1': {
                        'name': 'eth1', 'prefix': host['host_ip'] + '/24', 'gateway': host['gateway']}}})
            self.assertEqual(json.loads((directory / 'intent.json').read_text()), manifest)


class ProcessVerifierTests(unittest.TestCase):
    def test_missing_or_malformed_results_raise_without_approval(self):
        invalid = ['', '{', 'null', '[]', '{}',
                   '{"accepted": "false", "details": ["bad"]}',
                   '{"accepted": true, "details": []}',
                   '{"accepted": true, "details": [1]}']
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'snapshot'
            directory.mkdir()
            verifier = ProcessVerifier([sys.executable, '-m', 'advnet.batfish_gate'])
            for stdout in invalid:
                with self.subTest(stdout=stdout), patch('advnet.trial.subprocess.run',
                        return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr='')):
                    with self.assertRaises((ValueError, AttributeError)):
                        verifier.verify(directory, Deadline(10))

    def test_failed_process_rejects_and_saves_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'snapshot'
            directory.mkdir()
            with patch('advnet.trial.subprocess.run', return_value=SimpleNamespace(
                    returncode=1, stdout='{"accepted":true,"details":["ignore"]}', stderr='real process error')):
                result = ProcessVerifier(['verifier']).verify(directory, Deadline(10))
            self.assertFalse(result['accepted'])
            self.assertTrue(result['process_error'])
            self.assertEqual((directory.parent / 'verifier-stderr.txt').read_text(), 'real process error')

    def test_invalid_candidate_response_aborts_trial_without_candidate_deployment(self):
        for stdout in ('', '{}', '{"accepted":"true","details":["invalid"]}'):
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / 'trial'
                lab = MemoryDiamondLab()
                responses = [SimpleNamespace(returncode=0, stdout='{"accepted":true,"details":["fixture"]}', stderr=''),
                             SimpleNamespace(returncode=0, stdout=stdout, stderr='')]
                with patch('advnet.trial.subprocess.run', side_effect=responses):
                    result = Trial(lab, FixedDecision(directory),
                                   ProcessVerifier(['verifier']), directory, max_attempts=1).run(repair_scenario())
                self.assertFalse(result['success'])
                self.assertIn('error_type', result)
                self.assertNotIn('deploy', [e['stage'] for e in result['events']])
                self.assertEqual([w['stage'] for w in lab.writes], ['injection', 'reset'])
                self.assertEqual(lab.state, lab.original)

    def test_verifier_timeout_aborts_trial_and_cleans_up_only_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'trial'
            lab = MemoryDiamondLab()
            with patch('advnet.trial.subprocess.run', side_effect=[
                    SimpleNamespace(returncode=0, stdout='{"accepted":true,"details":["fixture"]}', stderr=''),
                    subprocess.TimeoutExpired('verifier', 1)]):
                result = Trial(lab, FixedDecision(directory), ProcessVerifier(['verifier']),
                               directory, max_attempts=1).run(repair_scenario())
            self.assertEqual(result['error_type'], 'TimeoutExpired')
            self.assertEqual([w['stage'] for w in lab.writes], ['injection', 'reset'])
            self.assertEqual(lab.state, lab.original)
            self.assertNotIn('deploy', [e['stage'] for e in result['events']])


@unittest.skipUnless(os.environ.get('RUN_BATFISH_INTEGRATION') == '1',
                     'Set RUN_BATFISH_INTEGRATION=1 to use real Batfish at localhost:9996')
class LiveTrialGateTests(unittest.TestCase):
    def test_real_trial_rejects_bad_candidate_without_deployment(self):
        from pybatfish.client.session import Session

        directory = ROOT / 'runs' / ('diamond-trial-gate-' +
                    datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=False)
        trial_directory = directory / 'trial'
        lab = MemoryDiamondLab()
        decision = FixedDecision(trial_directory)
        gate_command = [sys.executable, '-m', 'advnet.batfish_gate']
        verifier = ProcessVerifier(gate_command)
        commands = []
        run_process = subprocess.run

        def gate_only(args, **kwargs):
            # Enforce before execution: no Docker, CLI, Containerlab or LLM process
            # is allowed. The actual gate subprocess and its responses are real.
            if args[:3] != gate_command or len(args) != 4:
                raise AssertionError(f'Forbidden subprocess: {args}')
            commands.append(args)
            return run_process(args, **kwargs)

        protected = [ROOT / 'advnet/lab.py', ROOT / 'labs/diamond.clab.yml',
                     *sorted((ROOT / 'labs/configs/diamond').glob('*.cfg')),
                     *sorted((ROOT / 'labs/configs/abilene').glob('*.cfg')),
                     *sorted(p for p in (ROOT / 'runs/diamond-snapshot').rglob('*') if p.is_file())]
        digests = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        report = {'server': 'localhost:9996', 'decision_source': 'predetermined Proposal fixture',
                  'measurements': 'synthetic in-memory Diamond lab', 'network_writes': 'in-memory only',
                  'integration_passed': False}
        try:
            bf = Session(host='localhost')
            bf.set_network('advnet-diamond')
            before = set(bf.list_snapshots())
            with patch.dict(os.environ, {'BATFISH_HOST': 'localhost'}), \
                    patch('advnet.trial.subprocess.run', side_effect=gate_only):
                result = Trial(lab, decision, verifier, trial_directory, max_attempts=2).run(repair_scenario())
            report.update(trial_result=result, subprocesses=commands, simulated_writes=lab.writes,
                          simulated_measurements=lab.measurements, feedback_seen=decision.feedback_seen)
            healthy = json.loads((trial_directory / 'healthy-verification.json').read_text())
            candidate = json.loads((trial_directory / 'attempt-1/verification.json').read_text())
            report.update(healthy_verification=healthy, candidate_verification=candidate)
            self.assertTrue(healthy['accepted'], healthy)
            self.assertFalse(candidate['accepted'], candidate)
            self.assertIn('host-a -> host-b: NEIGHBOR_UNREACHABLE', candidate['details'])
            self.assertIn('host-b -> host-a: NO_ROUTE', candidate['details'])
            self.assertNotIn('error_type', result)
            self.assertFalse(result['success'])  # The unsafe repair was never accepted.
            self.assertEqual(result['model_calls'], [])
            self.assertEqual(len(commands), 2)  # Healthy gate, then candidate gate.
            stages = [e['stage'] for e in result['events']]
            self.assertNotIn('deploy', stages)
            self.assertNotIn('rollback_candidate', stages)
            self.assertIn('inject_fault', stages)
            self.assertIn('harness_reset_not_a_repair', stages)
            self.assertEqual([w['stage'] for w in lab.writes], ['injection', 'reset'])
            self.assertEqual(lab.state, lab.original)
            self.assertTrue(result['cleanup_succeeded'])
            feedback = json.loads((trial_directory / 'attempt-2/feedback.json').read_text())
            self.assertEqual(feedback[0]['stage'], 'verification')
            self.assertIs(feedback[0]['accepted'], False)
            self.assertEqual(feedback[0]['details'], candidate['details'])
            self.assertTrue(lab.measurements[1]['all_reachable'])  # Alternate path still exists.
            self.assertFalse(lab.measurements[1]['ospf_healthy'])  # Repair precondition holds.

            created = set(bf.list_snapshots()) - before
            self.assertEqual(len(created), 2)
            report['candidate_snapshot'] = bf.set_snapshot(index=0)
            self.assertIn(report['candidate_snapshot'], created)
            candidate_directory = trial_directory / 'attempt-1/snapshot'
            self.assertEqual(bf.get_snapshot_input_object_text('configs/r1.cfg'),
                             (candidate_directory / 'configs/r1.cfg').read_text())
            parsed = bf.q.fileParseStatus().answer().frame()
            issues = bf.q.initIssues().answer().frame()
            nodes = bf.q.nodeProperties().answer().frame()
            report.update(parse_results=parsed.to_dict('records'), initialization_issues=issues.to_dict('records'),
                          modeled_nodes=sorted(str(n) for n in nodes['Node']))
            expected = {**{f'configs/r{i}.cfg': 'ARISTA' for i in range(1, 5)},
                        **{f'hosts/{name}.json': 'HOST' for name in ('host-a', 'host-b')}}
            self.assertEqual(len(parsed), 6)
            self.assertEqual(set(parsed['File_Name']), set(expected))
            for row in parsed.itertuples():
                self.assertEqual(row.Status, 'PASSED')
                self.assertEqual(row.File_Format, expected[row.File_Name])
            self.assertTrue(issues.empty, report['initialization_issues'])
            self.assertEqual(set(nodes['Node']), set(lab.state) | set(lab.manifest['hosts']))
            report['protected_files_unchanged'] = all(
                hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in digests.items())
            self.assertTrue(report['protected_files_unchanged'])
            report['integration_passed'] = True
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            dump(directory / 'evidence.json', report)
            print(f'\nLive controller evidence: {directory / "evidence.json"}', flush=True)


if __name__ == '__main__':
    unittest.main()
