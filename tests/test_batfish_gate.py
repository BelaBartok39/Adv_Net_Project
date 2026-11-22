"""Offline SDK response fixtures; these tests do not claim live Batfish validation."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from advnet.batfish_gate import check, host_addresses, main, model_config
from scripts.build_diamond_snapshot import ROOT, build_snapshot, disable_interface, set_ospf_cost


class Frame:
    """Small tabular response fixture; no optional SDK/pandas install is needed."""
    def __init__(self, data=(), columns=None):
        if isinstance(data, dict):
            columns = list(data)
            data = [dict(zip(columns, values)) for values in zip(*data.values())]
        self.rows = list(data)
        self.columns = set(columns or (self.rows[0] if self.rows else ()))
        self.empty = not self.rows

    def __getitem__(self, column):
        if column not in self.columns:
            raise KeyError(column)
        return [row[column] for row in self.rows]

    def itertuples(self):
        return iter(SimpleNamespace(**row) for row in self.rows)

    def to_dict(self, orient):
        assert orient == 'records'
        return self.rows


def answer(frame):
    result = MagicMock()
    result.answer.return_value.frame.return_value = frame
    return result


def trace_row(source, destination, hosts, routers):
    flow = SimpleNamespace(ingressNode=source, srcIp=hosts[source],
                           dstIp=hosts[destination], ipProtocol='ICMP')
    trace = SimpleNamespace(disposition='ACCEPTED', hops=[
        SimpleNamespace(node=name) for name in (source, *routers, destination)])
    return {'Flow': flow, 'Traces': [trace], 'TraceCount': 1}


class DiamondSnapshotTests(unittest.TestCase):
    def test_via_r3_changes_only_two_costs_and_adds_policy_explicitly(self):
        originals = {p: p.read_bytes() for p in (ROOT / 'labs/configs/diamond').glob('*.cfg')}
        with tempfile.TemporaryDirectory() as tmp:
            healthy = build_snapshot(Path(tmp) / 'healthy', path_policy=True)
            alternate = build_snapshot(Path(tmp) / 'alternate', path_policy=True, via_r3=True)
            for path in healthy.rglob('*'):
                if not path.is_file():
                    continue
                relative = path.relative_to(healthy)
                expected = path.read_bytes()
                if str(relative) in ('configs/r1.cfg', 'configs/r4.cfg'):
                    expected = expected.replace(b'   ip ospf cost 10\n', b'   ip ospf cost 1000\n')
                self.assertEqual((alternate / relative).read_bytes(), expected)
            self.assertEqual({p: p.read_bytes() for p in originals}, originals)
            manifest = json.loads((healthy / 'intent.json').read_text())
            self.assertEqual(manifest['path_policies'][0]['required_transit_routers'], ['r1', 'r2', 'r4'])
            self.assertEqual(manifest['path_policies'][0]['forbidden_transit_routers'], ['r3'])
            with self.assertRaises(FileExistsError):
                build_snapshot(healthy, via_r3=True, path_policy=True)

    def test_path_policy_and_via_r3_cli(self):
        from scripts.build_diamond_snapshot import main as build_main
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'alternate'
            with patch('sys.argv', ['build_diamond_snapshot.py', '--via-r3', '--path-policy',
                                    '--output', str(directory)]), redirect_stdout(io.StringIO()):
                build_main()
            self.assertIn('   ip ospf cost 1000\n', (directory / 'configs/r1.cfg').read_text())
            self.assertIn('path_policies', json.loads((directory / 'intent.json').read_text()))

    def test_cost_change_rejects_missing_or_ambiguous_costs_and_interfaces(self):
        valid = 'interface Ethernet1\n   ip ospf cost 10\n!\n'
        for text in ('', valid + valid, valid.replace('   ip ospf cost 10\n', ''),
                     valid.replace('   ip ospf cost 10\n', '   ip ospf cost 10\n   ip ospf cost 20\n')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                set_ospf_cost(text, 'Ethernet1', 1000)

    def test_model_export_preserves_routing_and_only_excludes_exact_default_guards(self):
        guards = ('system l1\n   unsupported speed action error\n'
                  '   unsupported error-correction action error\n')
        routing = 'interface Ethernet3\n   shutdown\n   ip address 10.10.1.1/24\n!\n'
        text = '> show running-config\n' + guards + '!\n' + routing
        modeled, excluded = model_config(text)
        self.assertEqual(len(excluded), 2)
        self.assertTrue(modeled.endswith(routing))
        self.assertNotIn('\nsystem l1\n', modeled)
        for unknown in (guards.replace('action error', 'action warn', 1),
                        guards + '   module unsupported-setting\n'):
            original = unknown + '!\n' + routing
            self.assertEqual(model_config(original), (original, []))

    def test_host_a_down_changes_only_r1_host_interface_in_separate_snapshot(self):
        originals = {p: p.read_bytes() for p in (ROOT / 'labs/configs/diamond').glob('*.cfg')}
        with tempfile.TemporaryDirectory() as tmp:
            healthy = build_snapshot(Path(tmp) / 'healthy')
            before = {p.relative_to(healthy): p.read_bytes() for p in healthy.rglob('*') if p.is_file()}
            broken = build_snapshot(Path(tmp) / 'broken', host_a_down=True)
            self.assertEqual({p.relative_to(broken) for p in broken.rglob('*') if p.is_file()}, set(before))
            for relative, content in before.items():
                self.assertEqual((healthy / relative).read_bytes(), content)
                actual = (broken / relative).read_bytes()
                if str(relative) == 'configs/r1.cfg':
                    old = b'   ip address 10.10.1.1/24\n   ip ospf area 0.0.0.0\n   no shutdown\n'
                    self.assertIn(old, content)
                    self.assertEqual(actual, content.replace(old, old.replace(b'no shutdown', b'shutdown')))
                    self.assertEqual(actual.count(b'\n   shutdown\n'), 1)
                else:
                    self.assertEqual(actual, content)
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)

    def test_host_a_down_refuses_to_overwrite_healthy_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = build_snapshot(Path(tmp) / 'healthy')
            before = (directory / 'configs/r1.cfg').read_bytes()
            with self.assertRaises(FileExistsError):
                build_snapshot(directory, host_a_down=True)
            self.assertEqual((directory / 'configs/r1.cfg').read_bytes(), before)

    def test_host_a_down_cli_builds_new_snapshot(self):
        from scripts.build_diamond_snapshot import main as build_main
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'broken'
            with patch('sys.argv', ['build_diamond_snapshot.py', '--host-a-down', '--output', str(directory)]), redirect_stdout(io.StringIO()):
                build_main()
            self.assertIn('   shutdown\n', (directory / 'configs/r1.cfg').read_text())

    def test_disabling_interface_rejects_missing_ambiguous_or_already_down_input(self):
        valid = 'interface Ethernet3\n   description to-host-a\n   no shutdown\n!\n'
        for text in ('interface Ethernet1\n   no shutdown\n!\n', valid + valid,
                     valid.replace('no shutdown', 'shutdown'), valid.replace('   no shutdown\n', '')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                disable_interface(text, 'Ethernet3')

    def test_snapshot_contents_and_baselines_unchanged(self):
        originals = {node: (ROOT / f'labs/configs/diamond/{node}.cfg').read_bytes()
                     for node in ('r1', 'r2', 'r3', 'r4')}
        with tempfile.TemporaryDirectory() as tmp:
            directory = build_snapshot(Path(tmp) / 'snapshot')
            self.assertEqual({p.name for p in directory.iterdir()}, {'configs', 'hosts', 'intent.json'})
            self.assertEqual({p.stem for p in (directory / 'configs').iterdir()}, set(originals))
            for node, original in originals.items():
                self.assertEqual((directory / f'configs/{node}.cfg').read_bytes(),
                                 f'! device: {node} (cEOSLab, EOS-4.36.3M)\n'.encode() + original)
                self.assertEqual((ROOT / f'labs/configs/diamond/{node}.cfg').read_bytes(), original)
            intent = json.loads((directory / 'intent.json').read_text())
            self.assertNotIn('path_policies', intent)
            self.assertEqual(intent['lab'], 'advnet-diamond')
            self.assertEqual(set(intent['routers']), set(originals))
            self.assertEqual(set(intent['hosts']), {'host-a', 'host-b'})
            self.assertEqual({p.name for p in (directory / 'hosts').iterdir()},
                             {'host-a.json', 'host-b.json'})
            for host, ip, gateway, router in [('host-a', '10.10.1.10', '10.10.1.1', 'r1'),
                                               ('host-b', '10.10.2.10', '10.10.2.1', 'r4')]:
                model = json.loads((directory / f'hosts/{host}.json').read_text())
                self.assertEqual(model, {'hostname': host, 'hostInterfaces': {'eth1': {
                    'name': 'eth1', 'prefix': ip + '/24', 'gateway': gateway}}})
                self.assertEqual(intent['hosts'][host]['router'], router)
                self.assertEqual(intent['hosts'][host]['router_interface'], 'Ethernet3')
                self.assertNotIn('172.30.100.', json.dumps(model))

    def test_refuses_to_overwrite_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = build_snapshot(Path(tmp) / 'snapshot')
            candidate = directory / 'configs/r1.cfg'
            candidate.write_text('existing uncommitted candidate')
            with self.assertRaises(FileExistsError):
                build_snapshot(directory)
            self.assertEqual(candidate.read_text(), 'existing uncommitted candidate')


class BatfishGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = build_snapshot(Path(self.tmp.name) / 'snapshot')
        self.manifest = json.loads((self.directory / 'intent.json').read_text())
        self.hosts = {'host-a': '10.10.1.10', 'host-b': '10.10.2.10'}
        self.rows = self.healthy_rows(self.hosts, ['r1', 'r2', 'r4'])
        self.bf = MagicMock()
        self.bf.q.fileParseStatus.return_value = answer(Frame([
            {'File_Name': 'configs/r1.cfg', 'Status': 'PASSED'}]))
        self.bf.q.initIssues.return_value = answer(Frame())
        self.set_nodes(set(self.manifest['routers']) | set(self.hosts))
        self.bf.q.traceroute.side_effect = lambda **kw: answer(
            Frame(self.rows[next(name for name, ip in self.hosts.items() if ip == kw['headers'].dstIps)],
                  columns=['Flow', 'Traces', 'TraceCount']))
        modules = {name: ModuleType(name) for name in (
            'pybatfish', 'pybatfish.client', 'pybatfish.client.session',
            'pybatfish.datamodel', 'pybatfish.datamodel.flow')}
        self.session = MagicMock(return_value=self.bf)
        modules['pybatfish.client.session'].Session = self.session
        modules['pybatfish.datamodel.flow'].HeaderConstraints = SimpleNamespace
        sdk = patch.dict(sys.modules, modules)
        sdk.start()
        self.addCleanup(sdk.stop)

    def healthy_rows(self, hosts, routers):
        return {destination: [trace_row(source, destination, hosts, routers)
                              for source in hosts if source != destination]
                for destination in hosts}

    def set_nodes(self, nodes):
        self.bf.q.nodeProperties.return_value = answer(Frame({'Node': sorted(nodes)}))

    def reject(self, text=None):
        result = check(self.directory)
        self.assertIs(result['accepted'], False)
        self.assertTrue(result['details'])
        if text:
            self.assertIn(text, ' '.join(result['details']))
        return result

    def write_policies(self, policies=None):
        self.manifest['path_policies'] = policies if policies is not None else [{
            'source': 'host-a', 'destination': 'host-b', 'ip_protocol': 'ICMP',
            'required_transit_routers': ['r1', 'r2', 'r4'],
            'forbidden_transit_routers': ['r3'],
        }]
        (self.directory / 'intent.json').write_text(json.dumps(self.manifest))

    def test_valid_policy_checks_every_path_and_reuses_icmp_queries(self):
        self.write_policies()
        result = check(self.directory)
        self.assertTrue(result['accepted'], result)
        self.assertEqual(self.bf.q.traceroute.call_count, 2)
        self.assertEqual(result['path_checks'][0]['path'], ['host-a', 'r1', 'r2', 'r4', 'host-b'])
        self.assertTrue(result['path_checks'][0]['accepted'])

    def test_one_violating_accepted_ecmp_path_rejects_with_full_diagnostics(self):
        self.write_policies()
        row = self.rows['host-b'][0]
        row['Traces'].append(trace_row('host-a', 'host-b', self.hosts, ['r1', 'r3', 'r4'])['Traces'][0])
        row['TraceCount'] = 2
        result = self.reject('violates path_policies[0]')
        details = ' '.join(result['details'])
        for expected in ('host-a -> host-b', 'host-a -> r1 -> r3 -> r4 -> host-b',
                         "missing required routers: ['r2']", "forbidden routers: ['r3']"):
            self.assertIn(expected, details)
        self.assertEqual([p['accepted'] for p in result['path_checks']], [True, False])
        self.assertFalse(any('got ' in d or 'gate error' in d for d in result['details']))

    def test_required_and_forbidden_routers_enforced_independently(self):
        for required, forbidden, failed in [(['r3'], [], True), ([], ['r2'], True),
                                             (['r2'], [], False), ([], ['r3'], False)]:
            with self.subTest(required=required, forbidden=forbidden):
                self.write_policies([{'source': 'host-a', 'destination': 'host-b', 'ip_protocol': 'ICMP',
                                      'required_transit_routers': required,
                                      'forbidden_transit_routers': forbidden}])
                result = check(self.directory)
                self.assertEqual(result['accepted'], not failed, result)

    def test_failed_alternative_does_not_hide_accepted_path_policy_violation(self):
        self.write_policies()
        row = self.rows['host-b'][0]
        row['Traces'] = trace_row('host-a', 'host-b', self.hosts, ['r1', 'r3', 'r4'])['Traces']
        failed = deepcopy(row['Traces'][0])
        failed.disposition = 'NO_ROUTE'
        failed.hops = [SimpleNamespace(node='host-a'), SimpleNamespace(node='r1')]
        row['Traces'].append(failed)
        row['TraceCount'] = 2
        result = self.reject('violates path_policies[0]')
        self.assertIn('NO_ROUTE', ' '.join(result['details']))
        self.assertIn('got 1', ' '.join(result['details']))

    def test_direction_and_all_matching_policies_are_enforced(self):
        self.write_policies()
        policies = self.manifest['path_policies']
        policies.append({**policies[0], 'required_transit_routers': ['r3'],
                         'forbidden_transit_routers': []})
        self.write_policies(policies)
        result = self.reject('path_policies[1]')
        self.assertEqual([p['accepted'] for p in result['path_checks']], [True, False])
        self.assertEqual(len(result['details']), 1)

    def test_empty_policies_preserve_legacy_queries_and_result(self):
        baseline = check(self.directory)
        self.bf.q.traceroute.reset_mock()
        self.write_policies([])
        self.assertEqual(check(self.directory), baseline)
        self.assertEqual(self.bf.q.traceroute.call_count, 2)

    def test_policy_flows_for_tcp_and_udp_are_queried_and_missing_flows_reject(self):
        for protocol in ('TCP', 'UDP'):
            with self.subTest(protocol=protocol):
                self.write_policies()
                self.write_policies([{**self.manifest['path_policies'][0], 'ip_protocol': protocol}])
                def query(**kw):
                    destination = next(h for h, ip in self.hosts.items() if ip == kw['headers'].dstIps)
                    rows = deepcopy(self.rows[destination])
                    for row in rows:
                        row['Flow'].ipProtocol = kw['headers'].ipProtocols[0]
                    return answer(Frame(rows, columns=['Flow', 'Traces', 'TraceCount']))
                self.bf.q.traceroute.side_effect = query
                result = check(self.directory)
                self.assertTrue(result['accepted'], result)
                self.assertIn('All 3 directed host flows', result['details'][0])
                self.assertTrue(any(c.kwargs['headers'].ipProtocols == [protocol]
                                    for c in self.bf.q.traceroute.call_args_list))
                original = query
                self.bf.q.traceroute.side_effect = lambda **kw: (
                    answer(Frame([], columns=['Flow', 'Traces', 'TraceCount']))
                    if kw['headers'].ipProtocols == [protocol] else original(**kw))
                self.reject('got 2')

    def test_policy_never_bypasses_connectivity_or_trace_completeness(self):
        self.write_policies()
        row = self.rows['host-b'][0]
        for count in (2, None):
            row['TraceCount'] = count
            self.reject('missing or truncated traces')
        row['TraceCount'] = 1
        row['Traces'][0].hops = []
        self.reject('hops')
        self.rows = self.healthy_rows(self.hosts, ['r1', 'r2', 'r4'])
        self.rows['host-a'][0]['Traces'][0].disposition = 'NO_ROUTE'
        self.reject('NO_ROUTE')

    def test_malformed_path_policies_reject_before_batfish_initialization(self):
        self.write_policies()
        valid = deepcopy(self.manifest['path_policies'][0])
        invalid = [None, {}, 'policy', [None], [{}], [{**valid, 'typo': True}]]
        invalid += [[{k: v for k, v in valid.items() if k != field}] for field in valid]
        for field, values in {
            'source': ['unknown', 'r1', [], None], 'destination': ['host-a', 'unknown', 1],
            'ip_protocol': ['bogus', '', None, [], 'ICMP,TCP', 'icmp'],
            'required_transit_routers': ['r2', None, ['host-a'], ['r2', 'r2'], [[]]],
            'forbidden_transit_routers': [False, {}, ['unknown'], ['r2']],
        }.items():
            invalid.extend([[{**valid, field: value}] for value in values])
        invalid.append([{**valid, 'required_transit_routers': [], 'forbidden_transit_routers': []}])
        for policies in invalid:
            with self.subTest(policies=policies):
                self.manifest['path_policies'] = policies
                (self.directory / 'intent.json').write_text(json.dumps(self.manifest))
                self.reject('path_policies')
        self.bf.init_snapshot.assert_not_called()

    def test_healthy_diamond_checks_exactly_two_cross_host_flows(self):
        result = check(self.directory)
        self.assertTrue(result['accepted'], result)
        self.assertIn('All 2 directed host flows', result['details'][0])
        self.bf.set_network.assert_called_once_with('advnet-diamond')
        self.bf.init_snapshot.assert_called_once()
        calls = self.bf.q.traceroute.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            kw = call.kwargs
            destination = next(h for h, ip in self.hosts.items() if ip == kw['headers'].dstIps)
            self.assertEqual(kw['startLocation'], json.dumps(next(h for h in self.hosts if h != destination)))
            self.assertEqual(kw['headers'].ipProtocols, ['ICMP'])
            self.assertIs(kw['ignoreFilters'], False)
            self.assertEqual(kw['maxTraces'], 10000)

    def test_normalized_model_does_not_modify_submitted_snapshot(self):
        path = self.directory / 'configs/r1.cfg'
        raw = ('> show running-config\nsystem l1\n   unsupported speed action error\n'
               '   unsupported error-correction action error\n!\n' + path.read_text())
        path.write_text(raw)
        uploaded = []
        def upload(directory, **kwargs):
            uploaded.append((Path(directory) / 'configs/r1.cfg').read_text())
        self.bf.init_snapshot.side_effect = upload
        result = check(self.directory)
        self.assertTrue(result['accepted'], result)
        self.assertIn('r1.cfg', result['model_exclusions'])
        self.assertEqual(path.read_text(), raw)
        self.assertIn('! EOS physical guard (not modeled): system l1', uploaded[0])

    def test_invalid_candidate_dispositions_reject(self):
        for disposition in ('NO_ROUTE', 'DENIED_IN', 'LOOP', 'EXITS_NETWORK', 'UNKNOWN',
                            'DELIVERED_TO_SUBNET', 'UNKNOWN.ACCEPTED'):
            with self.subTest(disposition=disposition):
                self.rows['host-b'][0]['Traces'][0].disposition = disposition
                self.reject(disposition)

    def test_broken_snapshot_connectivity_rejection_is_not_a_parse_failure(self):
        self.directory = build_snapshot(Path(self.tmp.name) / 'host-a-down', host_a_down=True)
        self.rows['host-b'][0]['Traces'][0].disposition = 'NEIGHBOR_UNREACHABLE'
        self.rows['host-a'][0]['Traces'][0].disposition = 'NO_ROUTE'
        result = self.reject('got 0')
        self.assertIn('host-a -> host-b: NEIGHBOR_UNREACHABLE', result['details'])
        self.assertIn('host-b -> host-a: NO_ROUTE', result['details'])
        self.assertFalse(any('parsing' in d or 'gate error' in d for d in result['details']))
        self.assertEqual(self.bf.q.traceroute.call_count, 2)

    def test_one_failed_path_among_accepted_paths_rejects(self):
        row = self.rows['host-b'][0]
        extra = deepcopy(row['Traces'][0])
        extra.disposition = 'NO_ROUTE'
        row['Traces'].append(extra)
        row['TraceCount'] = 2
        self.reject('NO_ROUTE')

    def test_missing_router_or_host_rejects(self):
        expected = set(self.manifest['routers']) | set(self.hosts)
        for node in sorted(expected):
            with self.subTest(node=node):
                self.set_nodes(expected - {node})
                self.reject(node)

    def test_parse_errors_and_missing_parse_results_reject(self):
        for status in ('FAILED', 'PARTIALLY_UNRECOGNIZED', 'UNKNOWN', None):
            with self.subTest(status=status):
                self.bf.q.fileParseStatus.return_value = answer(Frame({'Status': [status]}))
                self.reject('parsing results')
        self.bf.q.fileParseStatus.return_value = answer(Frame())
        self.reject('parsing results')

    def test_initialization_issues_reject_with_real_diagnostics(self):
        self.bf.q.initIssues.return_value = answer(Frame([{'Details': 'Unsupported EOS feature'}]))
        self.reject('Unsupported EOS feature')

    def test_missing_flow_rejects_even_with_other_direction_healthy(self):
        self.rows['host-b'] = []
        self.reject('got 1')

    def test_self_flows_cannot_replace_cross_host_checks(self):
        for host in self.hosts:
            self.rows[host] = [trace_row(host, host, self.hosts, ['r1'])]
        self.reject('got 0')

    def test_duplicate_flows_cannot_hide_missing_reverse_direction(self):
        self.rows['host-b'] *= 2
        self.rows['host-a'] = []
        self.reject('duplicate source flow')

    def test_missing_truncated_or_invalid_trace_count_rejects(self):
        row = self.rows['host-b'][0]
        for count in (0, 2, 10001, None, True, '1', 1.0):
            with self.subTest(count=count):
                row['TraceCount'] = count
                self.reject('traces')
        row['TraceCount'] = 0
        row['Traces'] = []
        self.reject('traces')

    def test_unexpected_source_destination_or_protocol_rejects(self):
        row = self.rows['host-b'][0]
        original = deepcopy(row['Flow'])
        for field, value in [('ingressNode', 'r1'), ('srcIp', '172.30.100.101'),
                             ('dstIp', '172.30.100.102'), ('ipProtocol', 'TCP')]:
            with self.subTest(field=field):
                row['Flow'] = deepcopy(original)
                setattr(row['Flow'], field, value)
                self.reject('Unexpected flow')

    def test_accepted_trace_must_reach_host_through_routers(self):
        trace = self.rows['host-b'][0]['Traces'][0]
        for path in ([], ['host-a', 'r1'], ['host-a', 'host-b'],
                     ['host-a', 'unknown', 'host-b']):
            with self.subTest(path=path):
                trace.hops = [SimpleNamespace(node=n) for n in path]
                self.reject('hops')

    def test_unavailable_or_unsupported_queries_reject_with_real_error(self):
        for method in ('set_network', 'init_snapshot'):
            with self.subTest(method=method):
                mock = getattr(self.bf, method)
                mock.side_effect = RuntimeError('Connection refused')
                self.reject('Connection refused')
                mock.side_effect = None
        for method in ('fileParseStatus', 'initIssues', 'nodeProperties', 'traceroute'):
            with self.subTest(method=method):
                mock = getattr(self.bf.q, method)
                old = mock.side_effect
                mock.side_effect = RuntimeError('Unsupported Batfish query: ' + method)
                self.reject('Unsupported Batfish query: ' + method)
                mock.side_effect = old

    def test_malformed_and_missing_response_frames_reject(self):
        for method in ('fileParseStatus', 'initIssues', 'nodeProperties', 'traceroute'):
            mock = getattr(self.bf.q, method)
            old = mock.side_effect
            for frame in (None, Frame({'WrongColumn': ['bad']})):
                with self.subTest(method=method, frame=type(frame).__name__):
                    mock.side_effect = None
                    mock.return_value = answer(frame)
                    self.reject()
            # Restore setUp's responses between independent query failures.
            if method == 'fileParseStatus':
                mock.return_value = answer(Frame({'Status': ['PASSED']}))
            elif method == 'initIssues':
                mock.return_value = answer(Frame())
            elif method == 'nodeProperties':
                self.set_nodes(set(self.manifest['routers']) | set(self.hosts))
            mock.side_effect = old

    def test_invalid_manifest_rejects_before_querying(self):
        for key, value in [('hosts', {}), ('routers', {}), ('lab', '')]:
            with self.subTest(key=key):
                manifest = deepcopy(self.manifest)
                manifest[key] = value
                (self.directory / 'intent.json').write_text(json.dumps(manifest))
                self.reject()
        self.bf.init_snapshot.assert_not_called()

    def test_cli_emits_json_on_service_failure(self):
        self.bf.init_snapshot.side_effect = RuntimeError('real initialization failure')
        stream = io.StringIO()
        with patch('sys.argv', ['batfish_gate', str(self.directory)]), redirect_stdout(stream):
            main()
        result = json.loads(stream.getvalue())
        self.assertFalse(result['accepted'])
        self.assertIn('real initialization failure', result['details'][0])

    def test_original_abilene_manifest_still_checks_all_110_directed_pairs(self):
        manifest = json.loads((ROOT / 'topologies/abilene.json').read_text())
        (self.directory / 'intent.json').write_text(json.dumps(manifest))
        hosts = {r['host']: r['host_ip'] for r in manifest['routers'].values()}
        self.hosts = hosts
        self.assertEqual(host_addresses(manifest), hosts)
        self.rows = self.healthy_rows(hosts, ['sea', 'den'])
        self.set_nodes(set(manifest['routers']) | set(hosts))
        result = check(self.directory)
        self.assertTrue(result['accepted'], result)
        self.assertIn('All 110 directed host flows', result['details'][0])
        self.bf.set_network.assert_called_once_with('advnet-abilene')
        for call in self.bf.q.traceroute.call_args_list:
            kw = call.kwargs
            destination = next(h for h, ip in hosts.items() if ip == kw['headers'].dstIps)
            self.assertEqual(kw['startLocation'], ','.join(json.dumps(h) for h in sorted(set(hosts) - {destination})))
        self.rows['h-sea'][0]['Traces'][0].disposition = 'NO_ROUTE'
        self.reject('NO_ROUTE')


if __name__ == '__main__':
    unittest.main()
