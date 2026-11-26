"""Offline demand-model tests: no scheduler, subprocesses, or network access."""
from copy import deepcopy
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from advnet.dynamic_workload import (
    DynamicFlow, DynamicWorkload, RateChange, ResolvedWorkload, TrafficBurst,
    WorkloadEvent, WorkloadGenerator, generate_workload, resolve_workload,
)
from advnet.scenarios import MAX_CONCURRENT_FLOWS


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'topologies/abilene.json').read_text())


def flow(flow_id='a', **overrides):
    return {'flow_id': flow_id, 'source': 'sea', 'destination': 'sva', 'offered_mbps': 5,
            'start_seconds': 0, 'stop_seconds': 20, **overrides}


def workload(flows=None, **overrides):
    return {'duration_seconds': 30, 'flows': [flow()] if flows is None else flows, **overrides}


def generator(**overrides):
    return {'duration_seconds': 30, 'seed': 7311, 'hosts': ['sea', 'sva', 'den', 'nyc'],
            'flow_count': 6, 'arrival_interval_seconds': 4, 'flow_duration_seconds': 8,
            'min_offered_mbps': 2, 'max_offered_mbps': 30, **overrides}


def events(resolved):
    return [(event.at_seconds, event.action, event.flow_id, event.offered_mbps)
            for event in resolved.events]


class DynamicWorkloadTests(unittest.TestCase):
    def test_static_flow_resolves_to_explicit_start_and_stop(self):
        resolved = resolve_workload(workload(), MANIFEST)
        self.assertEqual(events(resolved), [(0, 'start', 'a', 5), (20, 'stop', 'a', None)])
        self.assertEqual([event.sequence for event in resolved.events], [0, 1])
        self.assertEqual(resolved.flows[0].destination_port, 5201)
        self.assertIsNone(resolved.seed)
        self.assertEqual(resolved.duration_seconds, 30)

    def test_arrivals_departures_sustained_rates_and_burst_restoration(self):
        demand = workload([flow(rate_changes=[{'at_seconds': 4, 'offered_mbps': 7},
                                             {'at_seconds': 9, 'offered_mbps': 10}],
                                bursts=[{'start_seconds': 8, 'stop_seconds': 12,
                                         'offered_mbps': 40}]),
                           flow('b', destination='nyc', start_seconds=6, stop_seconds=15)])
        self.assertEqual(events(resolve_workload(demand, MANIFEST)), [
            (0, 'start', 'a', 5), (4, 'set_rate', 'a', 7), (6, 'start', 'b', 5),
            (8, 'set_rate', 'a', 40), (12, 'set_rate', 'a', 10),
            (15, 'stop', 'b', None), (20, 'stop', 'a', None)])

    def test_same_timestamp_order_is_explicit_and_uses_input_flow_order(self):
        demand = workload([
            flow('z', stop_seconds=5),
            flow('b', destination='nyc', rate_changes=[{'at_seconds': 5, 'offered_mbps': 6}]),
            flow('a', destination='den', rate_changes=[{'at_seconds': 5, 'offered_mbps': 7}]),
            flow('new-z', start_seconds=5),
            flow('new-a', destination='lax', start_seconds=5)])
        resolved = resolve_workload(demand, MANIFEST)
        at_five = [event for event in events(resolved) if event[0] == 5]
        self.assertEqual(at_five, [(5, 'stop', 'z', None), (5, 'set_rate', 'b', 6),
                                  (5, 'set_rate', 'a', 7), (5, 'start', 'new-z', 5),
                                  (5, 'start', 'new-a', 5)])
        self.assertEqual([e.sequence for e in resolved.events], list(range(len(resolved.events))))

    def test_fractional_seconds_and_rates_are_preserved(self):
        demand = workload([flow(start_seconds=0.125, stop_seconds=10.875, offered_mbps=0.01,
                                rate_changes=[{'at_seconds': 1.25, 'offered_mbps': 1000}])],
                          duration_seconds=11.5)
        self.assertEqual(events(resolve_workload(demand, MANIFEST)), [
            (0.125, 'start', 'a', 0.01), (1.25, 'set_rate', 'a', 1000),
            (10.875, 'stop', 'a', None)])

    def test_bursts_at_lifetime_edges_do_not_create_redundant_events(self):
        demand = workload([flow(bursts=[{'start_seconds': 0, 'stop_seconds': 5, 'offered_mbps': 20},
                                        {'start_seconds': 5, 'stop_seconds': 20, 'offered_mbps': 30}])])
        self.assertEqual(events(resolve_workload(demand, MANIFEST)), [
            (0, 'start', 'a', 20), (5, 'set_rate', 'a', 30), (20, 'stop', 'a', None)])

    def test_coincident_rate_change_and_burst_boundaries_have_one_effective_rate(self):
        demand = workload([flow(rate_changes=[{'at_seconds': 4, 'offered_mbps': 7},
                                             {'at_seconds': 8, 'offered_mbps': 9}],
                                bursts=[{'start_seconds': 4, 'stop_seconds': 8, 'offered_mbps': 20}])])
        self.assertEqual(events(resolve_workload(demand, MANIFEST)), [
            (0, 'start', 'a', 5), (4, 'set_rate', 'a', 20),
            (8, 'set_rate', 'a', 9), (20, 'stop', 'a', None)])

    def test_noop_updates_are_omitted_but_input_configuration_is_preserved(self):
        changes = [{'at_seconds': 4, 'offered_mbps': 5}]
        resolved = resolve_workload(workload([flow(rate_changes=changes)]), MANIFEST)
        self.assertEqual(len(resolved.events), 2)
        self.assertEqual(resolved.flows[0].rate_changes[0].at_seconds, 4)

    def test_unknown_hosts_and_same_endpoints_reject(self):
        for values in ({'source': 'unknown'}, {'destination': 'h-sva'},
                       {'destination': 'sea'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_workload(workload([flow(**values)]), MANIFEST)

    def test_duplicate_ids_reject_even_for_disjoint_lifetimes(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate flow IDs'):
            resolve_workload(workload([flow(stop_seconds=10), flow(start_seconds=10)]), MANIFEST)

    def test_invalid_flow_identifiers_reject(self):
        for value in ('', 'white space', '../a', 3, True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                DynamicFlow.model_validate(flow(value))

    def test_rates_are_strict_finite_positive_and_bounded_in_every_schema(self):
        for rate in (0, -1, 1000.01, float('nan'), float('inf'), float('-inf'), True, '5', None):
            cases = [(DynamicFlow, flow(offered_mbps=rate)),
                     (RateChange, {'at_seconds': 1, 'offered_mbps': rate}),
                     (TrafficBurst, {'start_seconds': 1, 'stop_seconds': 2, 'offered_mbps': rate}),
                     (WorkloadEvent, {'sequence': 0, 'at_seconds': 0, 'flow_id': 'a',
                                      'action': 'start', 'offered_mbps': rate}),
                     (WorkloadGenerator, generator(min_offered_mbps=rate)),
                     (WorkloadGenerator, generator(max_offered_mbps=rate))]
            for model, value in cases:
                with self.subTest(rate=rate, model=model.__name__), self.assertRaises(ValidationError):
                    model.model_validate(value)

    def test_times_are_strict_finite_and_nonnegative(self):
        for at in (-1, float('nan'), float('inf'), True, '1', None):
            cases = [(DynamicFlow, flow(start_seconds=at)),
                     (DynamicFlow, flow(stop_seconds=at)),
                     (RateChange, {'at_seconds': at, 'offered_mbps': 5}),
                     (TrafficBurst, {'start_seconds': at, 'stop_seconds': 2, 'offered_mbps': 5}),
                     (TrafficBurst, {'start_seconds': 0, 'stop_seconds': at, 'offered_mbps': 5}),
                     (WorkloadEvent, {'sequence': 0, 'at_seconds': at, 'flow_id': 'a',
                                      'action': 'start', 'offered_mbps': 5}),
                     (DynamicWorkload, workload(duration_seconds=at))]
            for model, value in cases:
                with self.subTest(at=at, model=model.__name__), self.assertRaises(ValidationError):
                    model.model_validate(value)

    def test_invalid_flow_and_experiment_windows_reject(self):
        for values in ({'start_seconds': 20}, {'start_seconds': 21}, {'stop_seconds': 0},
                       {'stop_seconds': 31}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_workload(workload([flow(**values)]), MANIFEST)
        for duration in (0, 86401):
            with self.subTest(duration=duration), self.assertRaises(ValidationError):
                DynamicWorkload.model_validate(workload(duration_seconds=duration))
        self.assertEqual(resolve_workload(workload(duration_seconds=20), MANIFEST).duration_seconds, 20)

    def test_rate_changes_must_be_strictly_ordered_inside_lifetime(self):
        for times in ([0], [20], [21], [5, 5], [6, 4], [1, 19, 3]):
            with self.subTest(times=times), self.assertRaisesRegex(ValueError, 'strictly ordered'):
                DynamicFlow.model_validate(flow(rate_changes=[{'at_seconds': at, 'offered_mbps': 10}
                                                               for at in times]))

    def test_invalid_burst_order_overlap_and_windows_reject(self):
        for windows in ([(2, 2)], [(3, 2)], [(0, 21)], [(4, 9), (8, 12)], [(10, 12), (2, 4)]):
            with self.subTest(windows=windows), self.assertRaises(ValueError):
                DynamicFlow.model_validate(flow(bursts=[{'start_seconds': a, 'stop_seconds': b,
                                                         'offered_mbps': 20} for a, b in windows]))
        with self.assertRaises(ValueError):
            DynamicFlow.model_validate(flow(start_seconds=2, bursts=[
                {'start_seconds': 1, 'stop_seconds': 3, 'offered_mbps': 20}]))

    def test_bursts_must_increase_the_sustained_rate_for_their_entire_window(self):
        for changes, burst_rate in (([], 5), ([], 4),
                                    ([{'at_seconds': 2, 'offered_mbps': 30}], 20),
                                    ([{'at_seconds': 5, 'offered_mbps': 30}], 20)):
            with self.subTest(changes=changes, rate=burst_rate), self.assertRaisesRegex(ValueError, 'exceed'):
                DynamicFlow.model_validate(flow(rate_changes=changes, bursts=[
                    {'start_seconds': 3, 'stop_seconds': 8, 'offered_mbps': burst_rate}]))

    def test_concurrency_limit_applies_to_active_flows_not_total_count(self):
        concurrent = [flow(str(i), destination_port=5201 + i) for i in range(MAX_CONCURRENT_FLOWS)]
        self.assertEqual(len(resolve_workload(workload(concurrent), MANIFEST).flows), 4)
        with self.assertRaisesRegex(ValueError, 'At most 4'):
            resolve_workload(workload(concurrent + [flow('fifth', destination_port=5300)]), MANIFEST)
        sequential = [flow(str(i), start_seconds=i * 3, stop_seconds=(i + 1) * 3) for i in range(10)]
        self.assertEqual(len(resolve_workload(workload(sequential), MANIFEST).flows), 10)

    def test_destination_port_conflicts_and_alternatives(self):
        with self.assertRaisesRegex(ValueError, 'destination host/port'):
            resolve_workload(workload([flow(), flow('b', source='den')]), MANIFEST)
        for second in (flow('b', destination_port=5202), flow('b', destination='nyc')):
            resolved = resolve_workload(workload([flow(), second]), MANIFEST)
            self.assertEqual(len(resolved.events), 4)
        for port in (0, 65536, True, '5201', 5201.0):
            with self.subTest(port=port), self.assertRaises(ValidationError):
                DynamicFlow.model_validate(flow(destination_port=port))
        for port in (1, 65535):
            self.assertEqual(DynamicFlow.model_validate(flow(destination_port=port)).destination_port, port)

    def test_inventory_aliases_cannot_bypass_destination_port_conflicts(self):
        manifest = deepcopy(MANIFEST)
        manifest['routers']['sva-alias'] = manifest['routers']['sva']
        with self.assertRaisesRegex(ValueError, 'destination host/port'):
            resolve_workload(workload([flow(), flow('b', destination='sva-alias')]), manifest)
        with self.assertRaisesRegex(ValueError, 'distinct physical hosts'):
            resolve_workload(workload([flow(source='sva-alias')]), manifest)

    def test_empty_workload_and_unknown_schema_reject(self):
        for demand in (workload([]), workload(schema_version='2'), workload(seed=True), workload(seed='2')):
            with self.subTest(demand=demand), self.assertRaises(ValidationError):
                DynamicWorkload.model_validate(demand)

    def test_configuration_and_schedule_reject_measurement_fields(self):
        for key in ('received_mbps', 'jitter_ms', 'actual_seconds', 'status'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                DynamicWorkload.model_validate(workload(**{key: 1}))
            with self.subTest(key=key), self.assertRaises(ValidationError):
                DynamicFlow.model_validate(flow(**{key: 1}))
            resolved = resolve_workload(workload(), MANIFEST).model_dump()
            resolved['events'][0][key] = 1
            with self.assertRaises(ValidationError):
                ResolvedWorkload.model_validate(resolved)

    def test_resolved_schedule_json_roundtrip_preserves_exact_events_and_seed(self):
        resolved = resolve_workload(workload(seed=42), MANIFEST)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resolved.json'
            path.write_text(resolved.model_dump_json(indent=2))
            loaded = ResolvedWorkload.model_validate_json(path.read_text()).validate_inventory(MANIFEST)
        self.assertEqual(resolved, loaded)
        json.dumps(loaded.model_dump(), allow_nan=False)

    def test_resolved_schedule_rejects_tampering_and_inconsistent_event_order(self):
        original = resolve_workload(workload(), MANIFEST).model_dump()
        for field, value in (('sequence', 2), ('at_seconds', 1), ('flow_id', 'unknown'),
                             ('action', 'set_rate'), ('offered_mbps', 6)):
            changed = deepcopy(original)
            changed['events'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ResolvedWorkload.model_validate(changed)
        for entries in (original['events'][::-1], original['events'][:1],
                        original['events'] + original['events']):
            with self.assertRaises(ValueError):
                ResolvedWorkload.model_validate({**original, 'events': entries})

    def test_event_rate_and_action_consistency(self):
        for action, rate in (('start', None), ('set_rate', None), ('stop', 5), ('unsupported', 5)):
            with self.subTest(action=action, rate=rate), self.assertRaises(ValidationError):
                WorkloadEvent(sequence=0, at_seconds=0, flow_id='a', action=action, offered_mbps=rate)

    def test_resolution_revalidates_mutated_models_and_does_not_mutate_inputs(self):
        original = workload()
        saved = deepcopy(original)
        resolve_workload(original, MANIFEST)
        self.assertEqual(original, saved)
        parsed = DynamicWorkload.model_validate(original)
        parsed.flows[0].offered_mbps = -1
        with self.assertRaises(ValidationError):
            resolve_workload(parsed, MANIFEST)


class GeneratedWorkloadTests(unittest.TestCase):
    def test_same_seed_inputs_and_inventory_produce_identical_explicit_schedules(self):
        spec = generator()
        before = deepcopy(spec)
        first = generate_workload(spec, MANIFEST)
        second = generate_workload(spec, MANIFEST)
        self.assertEqual(first, second)
        self.assertEqual(resolve_workload(first, MANIFEST), resolve_workload(second, MANIFEST))
        self.assertEqual(first.seed, 7311)
        self.assertEqual(spec, before)
        self.assertEqual([f.flow_id for f in first.flows], [f'generated-{i}' for i in range(1, 7)])
        self.assertEqual([f.start_seconds for f in first.flows], [0, 4, 8, 12, 16, 20])
        self.assertTrue(all(2 <= f.offered_mbps <= 30 and f.stop_seconds - f.start_seconds == 8
                            for f in first.flows))

    def test_seed_changes_workload_without_changing_global_rng_state(self):
        state = random.getstate()
        first = generate_workload(generator(seed=1), MANIFEST)
        second = generate_workload(generator(seed=2), MANIFEST)
        generate_workload(generator(seed=None), MANIFEST)
        self.assertEqual(state, random.getstate())
        self.assertNotEqual(first.flows, second.flows)

    def test_sorted_host_candidates_make_input_insertion_order_irrelevant(self):
        spec = generator()
        reversed_spec = {**spec, 'hosts': spec['hosts'][::-1]}
        reversed_manifest = {**MANIFEST, 'routers': dict(reversed(list(MANIFEST['routers'].items())))}
        self.assertEqual(generate_workload(spec, MANIFEST), generate_workload(reversed_spec, reversed_manifest))

    def test_seedless_output_can_be_saved_exactly_without_regeneration(self):
        generated = generate_workload(generator(seed=None), MANIFEST)
        resolved = resolve_workload(generated, MANIFEST)
        loaded = ResolvedWorkload.model_validate_json(resolved.model_dump_json()).validate_inventory(MANIFEST)
        self.assertEqual(resolved.events, loaded.events)
        self.assertIsNone(loaded.seed)

    def test_generator_supports_model_input_zero_seed_and_fixed_rate_bounds(self):
        spec = WorkloadGenerator.model_validate(generator(seed=0, min_offered_mbps=5, max_offered_mbps=5))
        generated = generate_workload(spec, MANIFEST)
        self.assertEqual(generated.seed, 0)
        self.assertEqual({f.offered_mbps for f in generated.flows}, {5})
        self.assertEqual(generated, generate_workload(spec, MANIFEST))

    def test_invalid_generation_parameters_and_hosts_reject(self):
        for overrides in ({'hosts': ['sea']}, {'hosts': ['sea', 'sea']}, {'hosts': ['sea', 'unknown']},
                          {'flow_count': 0}, {'flow_count': True}, {'seed': True}, {'seed': '3'},
                          {'min_offered_mbps': 40}, {'arrival_interval_seconds': 0},
                          {'arrival_interval_seconds': float('inf')}, {'flow_duration_seconds': 0},
                          {'flow_duration_seconds': float('nan')}, {'duration_seconds': 20},
                          {'destination_port': 65536}, {'flow_count': 2.5}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                generate_workload(generator(**overrides), MANIFEST)

    def test_generator_rejects_unsupported_overlap_and_does_not_shift_arrivals(self):
        with self.assertRaisesRegex(ValueError, 'At most 4'):
            generate_workload(generator(hosts=list(MANIFEST['routers']), flow_count=5,
                                        arrival_interval_seconds=1, flow_duration_seconds=10), MANIFEST)
        with self.assertRaisesRegex(ValueError, 'No free destination host/port'):
            generate_workload(generator(hosts=['sea', 'sva'], flow_count=3,
                                        arrival_interval_seconds=1, flow_duration_seconds=10), MANIFEST)

    def test_generator_can_overlap_four_and_reuse_endpoints_after_departure(self):
        four = generate_workload(generator(flow_count=4, arrival_interval_seconds=1,
                                            flow_duration_seconds=10), MANIFEST)
        self.assertEqual(len({f.destination for f in four.flows}), 4)
        sequential = generate_workload(generator(hosts=['sea', 'sva'], flow_count=10,
                                                 arrival_interval_seconds=2, flow_duration_seconds=2,
                                                 destination_port=5202), MANIFEST)
        self.assertEqual(len(sequential.flows), 10)
        self.assertEqual({f.destination_port for f in sequential.flows}, {5202})
        resolve_workload(sequential, MANIFEST)

    def test_model_operations_never_invoke_subprocesses(self):
        with patch('subprocess.run', side_effect=AssertionError('Network operation')), \
                patch('subprocess.Popen', side_effect=AssertionError('Network operation')):
            resolved = resolve_workload(generate_workload(generator(), MANIFEST), MANIFEST)
            ResolvedWorkload.model_validate_json(resolved.model_dump_json()).validate_inventory(MANIFEST)


if __name__ == '__main__':
    unittest.main()
