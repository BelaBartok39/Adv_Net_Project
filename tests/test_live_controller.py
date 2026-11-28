"""Live collector contracts and continuous UDP window regression tests."""
import argparse
from contextlib import contextmanager, nullcontext, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from advnet.controller import duration, main
from advnet.controller_demo import DemoClock, DemoNetwork
from advnet.controller_execution import RuntimeMeasurementError, fingerprint
from advnet.controller_live import LiveCollector, tbf_rates
from advnet.controller_traffic import RECEIVER_GRACE_SECONDS, ReceiverTraffic, demand_id, reference_profiles
from advnet.dynamic_traffic import load_schedule
from advnet.dynamic_workload import DynamicFlow
from advnet.eos import normalize_readback


class LiveControllerTests(unittest.TestCase):
    def test_duration_and_workload_profiles(self):
        self.assertEqual(duration('20m'), 1200)
        self.assertEqual(duration('1.5h'), 5400)
        with self.assertRaises(argparse.ArgumentTypeError):
            duration('0m')
        manifest = json.loads(Path('topologies/abilene.json').read_text())
        schedule, _ = load_schedule(Path('experiments/continuous/sea-sva.json').read_bytes(),
                                    manifest)
        profiles = reference_profiles(schedule)
        self.assertEqual(set(tuple(sorted(rate for _, rate in flows)) for flows in profiles.values()),
                         {(), (10, 15), (40, 50)})
        self.assertEqual(schedule.duration_seconds, 1200)

    def test_eos_readback_reorders_supported_settings_and_omits_explicit_default(self):
        rendered = ('interface Ethernet3\n   description host\n   ip ospf cost 20\n'
                    '   ip ospf area 0.0.0.0\n!\n')
        candidate = ('interface Ethernet3\n   description host\n   ip ospf area 0.0.0.0\n'
                     '   no shutdown\n   ip ospf cost 20\n!\n')
        self.assertEqual(normalize_readback({'sea': rendered}), normalize_readback({'sea': candidate}))
        self.assertNotEqual(normalize_readback({'sea': rendered}),
                            normalize_readback({'sea': candidate.replace('no shutdown', 'shutdown')}))
        self.assertNotEqual(normalize_readback({'sea': rendered}),
                            normalize_readback({'sea': candidate.replace('description host', 'description other')}))

    def test_tbf_rates_require_a_measured_directional_root_shaper(self):
        rows = [
            {'dev': 'eth1', 'root': True, 'kind': 'tbf', 'options': {'rate': 12500000}},
            {'dev': 'eth2', 'root': True, 'kind': 'noqueue'},
            {'dev': 'eth3', 'root': False, 'kind': 'tbf', 'options': {'rate': 12500000}},
        ]
        self.assertEqual(tbf_rates(rows, {f'eth{i}': f'sea:Ethernet{i}' for i in (1, 2, 3)}),
                         {'sea:Ethernet1': 100})

    def test_live_frame_uses_probed_health_and_rejects_incomparable_runtime(self):
        clock = DemoClock()
        inventory = {name: {'host': f'h-{name}', 'host_ip': f'127.0.0.{i}',
                            'host_interface': 'Ethernet2', 'interfaces': ['Ethernet1', 'Ethernet2']}
                     for i, name in enumerate(('sea', 'sva'), 1)}
        configs = {name: 'interface Ethernet1\n!' for name in inventory}
        manifest = {'links': [{'a': 'sea', 'a_interface': 'Ethernet1', 'b': 'sva',
                               'b_interface': 'Ethernet1'}], 'requirements': ['Host reachability']}

        class LabFixture:
            routers = inventory
            failed = False
            rtt = 1.5

            def expected_neighbors(self):
                return {'sea': {'10.0.0.2'}, 'sva': {'10.0.0.1'}}

            def configs(self, deadline):
                return configs.copy()

            def counters(self, deadline):
                return {name: {'Ethernet1': round(clock.seconds * 1e7)} for name in inventory}

            def ping_matrix(self, deadline, **options):
                self.probe_options = options
                return [{'source': a, 'destination': b, 'reachable': a != 'sea' or not self.failed,
                         'loss_fraction': float(a == 'sea' and self.failed), 'rtt_avg_ms': self.rtt}
                        for a, b in (('sea', 'sva'), ('sva', 'sea'))]

            def neighbors(self, deadline):
                return {'sea': '10.0.0.2 FULL', 'sva': '10.0.0.1 FULL'}

        lab = LabFixture()
        lab.manifest = manifest
        caps = SimpleNamespace(capacities=lambda *_: {name + ':Ethernet1': 100 for name in inventory})
        profile = [{'source': 'sea', 'destination': 'sva', 'destination_port': 5201, 'offered_mbps': 30.0}]
        rows = [{**profile[0], 'received_mbps': 29.5, 'loss_fraction': 0.016}]
        traffic = SimpleNamespace(begin=lambda seconds: SimpleNamespace(end=clock.seconds + seconds,
                                                                        epoch='e1', profile=profile),
                                  finish=lambda *_, **__: rows.copy(),
                                  current=lambda: ('e1', demand_id(profile)))
        deadline = SimpleNamespace(remaining=lambda: 60)
        collector = LiveCollector(lab, caps, traffic, fingerprint, 'test', clock, 4)
        collector._wait_until = lambda until, _: setattr(clock, 'seconds', max(until, clock.seconds))
        baseline = collector.window('before', 'optimization', deadline)
        self.assertEqual(lab.probe_options, {'count': 1, 'workers': 4})
        self.assertEqual(baseline.traffic['received_mbps'], 29.5)
        self.assertAlmostEqual(baseline.traffic['max_link_utilization'], 0.8)
        self.assertIsNone(collector('generation-1', deadline).signal.performance)
        collector.baselines[baseline.demand_id] = baseline
        frame = collector('generation-1', deadline)
        self.assertTrue(frame.signal.performance.healthy)
        self.assertAlmostEqual(frame.runtime['performance']['latency_limit_ms'], 6.875)
        self.assertEqual(frame.model_input.measurements['metric_definitions']['mean_rtt_ms']['required_pair_count'], 2)
        commands = []
        lab.cli = lambda node, cmd, deadline: (commands.append(cmd) or 'fixture route')
        requested = collector.observe_requests(['routing_tables', 'refresh_telemetry'], 'generation-1', deadline)
        evidence = requested.model_input.measurements['requested_observations']
        self.assertEqual(set(evidence['routing_tables']['routes']), {'sea', 'sva'})
        self.assertEqual(commands, ['show ip route', 'show ip route'])
        self.assertEqual(len(evidence['refresh_telemetry']['pairs']), 2)
        lab.cli = lambda *args: 'x' * 8193
        with self.assertRaisesRegex(ValueError, 'size limit'):
            collector.observe_requests(['routing_tables'], 'generation-1', deadline)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            collector.observe_requests(['show running-config'], 'generation-1', deadline)
        def changed_config(*args):
            configs['sea'] += '! external\n'
            return 'fixture route'
        lab.cli = changed_config
        with self.assertRaisesRegex(ValueError, 'scope changed'):
            collector.observe_requests(['routing_tables'], 'generation-1', deadline)
        configs['sea'] = configs['sva']
        lab.rtt = 20
        frame = collector('generation-1', deadline)
        self.assertFalse(frame.signal.performance.healthy)
        self.assertTrue(frame.signal.reachability.healthy)
        self.assertTrue(any('latency' in detail for detail in frame.runtime['performance']['details']))
        lab.rtt = 1.5
        rows[0].update(received_mbps=20, loss_fraction=0.1)
        frame = collector('generation-1', deadline)
        self.assertFalse(frame.signal.performance.healthy)
        self.assertTrue(any('throughput' in detail for detail in frame.runtime['performance']['details']))
        self.assertTrue(any('loss' in detail for detail in frame.runtime['performance']['details']))
        rows[0].update(received_mbps=29.5, loss_fraction=0.016)
        collector.baselines = {'other-demand': baseline}
        self.assertIsNone(collector('generation-1', deadline).signal.performance.healthy)
        collector.baselines = {baseline.demand_id: baseline}
        lab.failed = True
        frame = collector('generation-1', deadline)
        self.assertFalse(frame.signal.reachability.healthy)
        self.assertTrue(frame.signal.performance.healthy)
        self.assertTrue(frame.signal.ospf.healthy)
        self.assertEqual(frame.signal.reachability.measurement_generation, 'generation-1')
        traffic.current = lambda: ('e2', demand_id(profile))
        with self.assertRaisesRegex(RuntimeMeasurementError, 'demand changed') as caught:
            collector.window('after', 'repair', deadline)
        self.assertIn('health_window', caught.exception.evidence)
        self.assertIn('pairs', caught.exception.evidence)
        self.assertNotIn('configs', caught.exception.evidence)

    def test_performance_checks_each_concurrent_receiver_and_matching_reference(self):
        net = DemoNetwork(DemoClock())
        limit = SimpleNamespace(remaining=lambda: 60)
        reference = net.window('healthy', 'repair', limit)
        flows = [dict(source='sea', destination=dest, destination_port=5201, offered_mbps=rate,
                      seconds=1, received_mbps=rate, loss_fraction=0)
                 for dest, rate in [('sva', 50), ('lax', 40)]]
        reference = reference.model_copy(update={
            'source': 'live', 'capacity_sha256': 'a' * 64, 'traffic_scope': 'concurrent_udp',
            'traffic': dict(mode='concurrent_udp', concurrency_verified=True,
                            overlap_window=dict(start_seconds=0, end_seconds=1),
                            max_link_utilization=.93, flows=flows)})
        self.assertTrue(reference.performance_check(reference)['accepted'])
        slow = reference.model_copy(update={'mean_rtt_ms': 30})
        details = slow.performance_check(reference)['details']
        self.assertEqual(len(details), 1)
        self.assertTrue(details[0].startswith('Network-wide'))
        self.assertNotIn("('sea',", details[0])
        # Aggregate delivery is insufficient: each receiver must keep its rate.
        bad_flows = [dict(flows[0], received_mbps=60), dict(flows[1], received_mbps=30)]
        impaired = reference.model_copy(update={'traffic': dict(reference.traffic, flows=bad_flows)})
        self.assertFalse(impaired.performance_check(reference)['accepted'])
        loss = reference.model_copy(update={'traffic': dict(reference.traffic,
                                     flows=[flows[0], dict(flows[1], loss_fraction=.03)])})
        self.assertFalse(loss.performance_check(reference)['accepted'])
        for changes in ({'capacity_sha256': None}, {'capacity_sha256': 'b' * 64},
                        {'demand_id': 'different-demand'}, {'mean_rtt_ms': None}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'Incomparable'):
                reference.model_copy(update=changes).performance_check(reference)

    def test_calibration_records_failed_health_and_stops_its_traffic(self):
        reference = DemoNetwork(DemoClock()).window('healthy', 'repair', SimpleNamespace(remaining=lambda: 60))
        for failure in ('host', 'receiver_window', None):
            with self.subTest(failure=failure):
                collector = LiveCollector.__new__(LiveCollector)
                collector.baselines = {}
                active = {}
                stops = []
                collector.traffic = SimpleNamespace(
                    active=active,
                    start=lambda flow, rate, **kw: active.update({flow: rate}),
                    stop=lambda flow, **kw: (stops.append(flow), active.pop(flow)))
                sample = SimpleNamespace(evidence={'flows': []}, health=SimpleNamespace(
                    failed_pairs=[('atl', 'sea')] if failure == 'host' else [],
                    mismatched_ospf=[], mean_rtt_ms=1, pairs=[]))
                collector._sample = lambda _: sample

                def runtime(_):
                    if failure == 'receiver_window':
                        raise ValueError('Receiver evidence unavailable')
                    return reference.model_copy(update={'all_reachable': failure != 'host'})

                collector._runtime = runtime
                events = []
                call = lambda: collector.calibrate(
                    {reference.demand_id: [('flow-a', 50)]}, reference.config_sha256,
                    SimpleNamespace(remaining=lambda: 60), recorder=lambda **entry: events.append(entry))
                if failure:
                    with self.assertRaisesRegex(ValueError, 'failed host pairs' if failure == 'host'
                                                else 'Receiver evidence unavailable'):
                        call()
                    self.assertFalse(collector.baselines)
                    self.assertFalse(events[0]['accepted'])
                else:
                    call()
                    self.assertTrue(events[0]['accepted'])
                    self.assertIn(reference.demand_id, collector.baselines)
                self.assertEqual(stops, ['flow-a'])
                self.assertFalse(active)
                self.assertEqual(events[0]['health']['failed_pairs'], sample.health.failed_pairs)

    def test_launcher_writes_live_session_summary_in_observe_only_mode(self):
        clock = DemoClock()
        network = DemoNetwork(clock)
        hook_events = []

        @contextmanager
        def hook(owner, collector, namespaces):
            hook_events.append('entered')
            try:
                yield
            finally:
                hook_events.append('cleaned')

        class CollectorFixture:
            health_probe_workers = 4
            observations = 0
            receiver_windows = 0
            window = network.window

            def __init__(self, *args):
                self.baselines = {}

            def __call__(self, generation, deadline):
                self.observations += 1
                frame = network.sample(generation, deadline)
                frame.source = 'live'
                return frame

        with TemporaryDirectory() as temp, redirect_stdout(StringIO()) as console:
            root = Path(temp)
            with (patch('advnet.controller.Clock', return_value=clock),
                  patch('advnet.controller.LabNamespaces.open',
                        return_value=nullcontext(SimpleNamespace(socket_at=None))),
                  patch('advnet.controller.LiveCollector', CollectorFixture),
                  patch('advnet.dynamic_udp.LOCK_PATH', root / 'test.lock')):
                output = root / 'run'
                code = main(['run', '--duration', '30s', '--observe-only', '--output', str(output)],
                            session_hook=hook)
            self.assertEqual(code, 0)
            self.assertEqual(hook_events, ['entered', 'cleaned'])
            summary = json.loads(console.getvalue())
            self.assertTrue(summary['observe_only'])
            self.assertTrue(summary['traffic_cleanup_succeeded'])
            self.assertEqual(summary['observations'], 3)
            self.assertEqual(json.loads((output / 'sessions' / summary['session'] / 'summary.json').read_text()),
                             summary)

    @unittest.skipUnless(os.environ.get('RUN_CONTROLLER_SOCKET_TEST') == '1', 'requires UDP sockets')
    def test_receiver_reports_live_delivery_and_rejects_a_rate_transition(self):
        address = '127.0.0.2'
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((address, 0))
            port = probe.getsockname()[1]
        lab = SimpleNamespace(routers={'sea': {'host': 'source', 'host_ip': '127.0.0.1'},
                                       'sva': {'host': 'destination', 'host_ip': address}})
        traffic = ReceiverTraffic(lab, lambda _: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        flow = DynamicFlow(flow_id='sample', source='sea', destination='sva', destination_port=port,
                           offered_mbps=2, start_seconds=0, stop_seconds=5)
        try:
            traffic.start(flow, 2, timeout_seconds=1)
            capture = traffic.begin(1)
            time.sleep(1 + RECEIVER_GRACE_SECONDS + 0.02)
            report, = traffic.finish(capture, measured=True)
            self.assertGreater(report['received_mbps'], 1.8)
            self.assertLess(report['loss_fraction'], 0.02)
            self.assertIsNone(traffic.poll(flow.flow_id, timeout_seconds=1))
            capture = traffic.begin(1)
            traffic.set_rate(flow.flow_id, 3, timeout_seconds=1)
            traffic.set_rate(flow.flow_id, 2, timeout_seconds=1)
            with self.assertRaisesRegex(ValueError, 'demand changed'):
                traffic.finish(capture, measured=True)
        finally:
            traffic.cleanup(timeout_seconds=1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((address, port))
