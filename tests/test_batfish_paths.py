"""Opt-in real Batfish checks; no Containerlab, router transport or LLM calls."""
import hashlib
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from advnet.batfish_gate import check, host_addresses
from advnet.trial import dump, snapshot
from scripts.build_diamond_snapshot import ROOT, build_snapshot


@unittest.skipUnless(os.environ.get('RUN_BATFISH_INTEGRATION') == '1',
                     'Set RUN_BATFISH_INTEGRATION=1 to use a real Batfish server')
class LivePathPolicyTests(unittest.TestCase):
    def test_real_diamond_paths_and_abilene_compatibility(self):
        from pybatfish.client.session import Session
        from pybatfish.datamodel.flow import HeaderConstraints

        output_root = Path(os.environ.get('ADVNET_TEST_OUTPUT_DIR', str(ROOT / 'runs')))
        directory = output_root / ('batfish-path-gate-' +
                    datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=False)
        protected = [ROOT / 'advnet/trial.py', ROOT / 'advnet/lab.py',
                     ROOT / 'labs/diamond.clab.yml', ROOT / 'topologies/abilene.json',
                     *sorted((ROOT / 'labs/configs/diamond').glob('*.cfg')),
                     *sorted((ROOT / 'labs/configs/abilene').glob('*.cfg'))]
        digests = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        report = {'server': os.environ.get('BATFISH_HOST', 'localhost') + ':9996',
                  'integration_passed': False, 'cases': {}, 'protected_sha256_before': digests}

        def verify(name, candidate):
            """Inspect the exact snapshot uploaded by the actual gate."""
            manifest = json.loads((candidate / 'intent.json').read_text())
            bf = Session(host=os.environ.get('BATFISH_HOST', 'localhost'))
            bf.set_network(manifest['lab'])
            before = set(bf.list_snapshots())
            evidence = {'gate': check(candidate), 'snapshot_directory': str(candidate.resolve())}
            report['cases'][name] = evidence
            created = set(bf.list_snapshots()) - before
            self.assertEqual(len(created), 1, evidence)
            identifier = created.pop()
            bf.set_snapshot(identifier)
            parsed = bf.q.fileParseStatus().answer().frame()
            issues = bf.q.initIssues().answer().frame()
            nodes = bf.q.nodeProperties().answer().frame()
            hosts = host_addresses(manifest)
            evidence.update(snapshot=identifier, network=manifest['lab'],
                            parse_results=parsed.to_dict('records'),
                            initialization_issues=issues.to_dict('records'),
                            modeled_nodes=sorted(str(n) for n in nodes['Node']), flows=[],
                            input_sha256={str(p.relative_to(candidate)): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in sorted(candidate.rglob('*')) if p.is_file()})
            expected_files = {f'configs/{r}.cfg' for r in manifest['routers']} | {f'hosts/{h}.json' for h in hosts}
            self.assertEqual(set(parsed['File_Name']), expected_files)
            self.assertTrue(all(s == 'PASSED' for s in parsed['Status']), evidence)
            self.assertTrue(issues.empty, evidence)
            self.assertEqual(set(nodes['Node']), set(manifest['routers']) | set(hosts))
            for destination, ip in hosts.items():
                sources = set(hosts) - {destination}
                frame = bf.q.traceroute(
                    startLocation=','.join(json.dumps(h) for h in sorted(sources)),
                    headers=HeaderConstraints(dstIps=ip, ipProtocols=['ICMP']),
                    maxTraces=10000, ignoreFilters=False).answer().frame()
                self.assertEqual({row.Flow.ingressNode for row in frame.itertuples()}, sources)
                for row in frame.itertuples():
                    self.assertGreater(row.TraceCount, 0)
                    self.assertEqual(row.TraceCount, len(row.Traces))
                    evidence['flows'].append({
                        'source': row.Flow.ingressNode, 'destination': destination,
                        'source_ip': str(row.Flow.srcIp), 'destination_ip': str(row.Flow.dstIp),
                        'ip_protocol': str(row.Flow.ipProtocol), 'trace_count': int(row.TraceCount),
                        'traces': [{'disposition': str(t.disposition), 'path': [h.node for h in t.hops]}
                                   for t in row.Traces]})
            return evidence

        def accepted_paths(evidence, transit):
            self.assertEqual(len(evidence['flows']), 2)
            for flow in evidence['flows']:
                route = transit if flow['source'] == 'host-a' else list(reversed(transit))
                for trace in flow['traces']:
                    self.assertEqual(trace['disposition'], 'ACCEPTED')
                    self.assertEqual(trace['path'], [flow['source'], *route, flow['destination']])

        try:
            healthy = verify('healthy', build_snapshot(directory / 'healthy/snapshot', path_policy=True))
            self.assertTrue(healthy['gate']['accepted'], healthy)
            accepted_paths(healthy, ['r1', 'r2', 'r4'])
            self.assertTrue(healthy['gate']['path_checks'])

            alternate = verify('path-violating', build_snapshot(
                directory / 'path-violating/snapshot', via_r3=True, path_policy=True))
            self.assertFalse(alternate['gate']['accepted'], alternate)
            accepted_paths(alternate, ['r1', 'r3', 'r4'])
            self.assertTrue(all('violates path_policies[0]' in d for d in alternate['gate']['details']))
            violation = alternate['gate']['path_checks'][0]
            self.assertEqual(violation['missing_required_routers'], ['r2'])
            self.assertEqual(violation['forbidden_routers'], ['r3'])

            connectivity = verify('alternate-connectivity', build_snapshot(
                directory / 'alternate-connectivity/snapshot', via_r3=True))
            self.assertTrue(connectivity['gate']['accepted'], connectivity)
            accepted_paths(connectivity, ['r1', 'r3', 'r4'])

            broken = verify('broken-connectivity', build_snapshot(
                directory / 'broken-connectivity/snapshot', host_a_down=True, path_policy=True))
            self.assertFalse(broken['gate']['accepted'], broken)
            self.assertIn('host-a -> host-b: NEIGHBOR_UNREACHABLE', broken['gate']['details'])
            self.assertIn('host-b -> host-a: NO_ROUTE', broken['gate']['details'])

            manifest = json.loads((ROOT / 'topologies/abilene.json').read_text())
            configs = {n: (ROOT / f'labs/configs/abilene/{n}.cfg').read_text() for n in manifest['routers']}
            abilene = verify('abilene', snapshot(directory / 'abilene/snapshot', configs, manifest))
            self.assertTrue(abilene['gate']['accepted'], abilene)
            self.assertEqual(len(abilene['flows']), 110)
            self.assertTrue(all(t['disposition'] == 'ACCEPTED'
                                for f in abilene['flows'] for t in f['traces']))
            report['protected_files_unchanged'] = all(
                hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in digests.items())
            self.assertTrue(report['protected_files_unchanged'])
            report['integration_passed'] = True
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            for name, evidence in report['cases'].items():
                dump(directory / name / 'evidence.json', evidence)
            dump(directory / 'evidence.json', report)
            print(f'\nReal Batfish path evidence: {directory / "evidence.json"}', flush=True)


if __name__ == '__main__':
    unittest.main()
