"""Controlled latency recovery on Abilene using the existing live controller.

Setup installs a known route split through Batfish and runtime acceptance.
The timed arms retain natural Jev/LLM decisions; the reference repair is used
only by the explicitly labelled validation arm. All arms restore the lab.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median
import sys
from threading import Event, Thread

from . import controller
from .contracts import Proposal
from .controller_execution import VerifiedExecutor, fingerprint, verified
from .controller_live import LabNamespaces, LiveCollector
from .controller_traffic import ReceiverTraffic, reference_profiles
from .dynamic_traffic import load_schedule
from .dynamic_udp import LOCK_PATH, _lab_lock
from .eos import candidate_configs, normalize_readback
from .lab import Deadline, Lab, command
from .persistent_controller import Clock, atomic_json
from .trial import ProcessVerifier, snapshot


ROOT = Path(__file__).resolve().parents[1]


def proposal(node, interface, cost):
    return Proposal(outcome='candidate', diagnosis='Controlled experiment setup/reference',
                    evidence=['Explicitly labelled experiment action'],
                    expected_effect='Split traffic or avoid the delayed directed link',
                    changes=[{'device': node, 'interface': interface,
                              'action': 'set_ospf_cost', 'value': cost}])


def workload(seconds):
    return {'duration_seconds': seconds, 'flows': [
        {'flow_id': 'sea-' + destination, 'source': 'sea', 'destination': destination,
         'offered_mbps': rate, 'start_seconds': 0, 'stop_seconds': seconds}
        for destination, rate in [('sva', 35), ('lax', 25)]]}


class LinkDelay:
    """Own one netem child of an existing TBF; never replace its root shaper."""
    handle = '7e01:'

    def __init__(self, lab, namespaces, delay_ms=200, *, execute=command):
        if type(delay_ms) is not int or not 100 <= delay_ms <= 300:
            raise ValueError('Delay must be an integer from 100 to 300 ms')
        self.execute = execute
        self.namespace = f'/proc/{os.getpid()}/fd/{namespaces.descriptors["den"]}'
        self.delay_ms, self.parent, self.root = delay_ms, None, None

    def tc(self, *args):
        return self.execute(['nsenter', '--net=' + self.namespace, '--', 'tc', *args], timeout=10)

    def rows(self):
        return json.loads(self.tc('-j', 'qdisc', 'show', 'dev', 'eth3'))

    def apply(self):
        rows = self.rows()
        roots = [r for r in rows if r.get('root') is True and r['kind'] == 'tbf']
        if len(roots) != 1 or len(rows) != 1:
            raise ValueError('Expected an unmodified TBF with no existing child qdisc')
        self.root = roots[0]
        options = self.root.get('options', {})
        if (options.get('rate') != 8750000 or options.get('burst') not in (32767, 32768)
                or options.get('lat') != 50000):
            raise ValueError('Expected the experiment TBF: 70 Mbps, 32 KB burst, 50 ms latency')
        self.parent = self.root['handle'] + '1'
        self.tc('qdisc', 'add', 'dev', 'eth3', 'parent', self.parent, 'handle', self.handle,
                'netem', 'limit', '10000', 'delay', str(self.delay_ms) + 'ms')
        rows = self.rows()
        if not any(r.get('kind') == 'netem' and r.get('handle') == self.handle
                   and r.get('parent') == self.parent
                   and r.get('options', {}).get('delay') == {
                       'delay': self.delay_ms / 1000, 'jitter': 0, 'correlation': 0}
                   and r.get('options', {}).get('limit') == 10000 for r in rows):
            raise ValueError('Injected delay readback mismatch')
        return rows

    def remove(self):
        if self.parent is None:
            return
        rows = self.rows()
        roots = [r for r in rows if r.get('root') is True]
        if (len(roots) != 1 or roots[0].get('kind') != 'tbf'
                or roots[0].get('handle') != self.root['handle']
                or roots[0].get('options') != self.root['options']):
            raise ValueError('Root shaping changed during the delay experiment')
        owned = [r for r in rows if r.get('handle') == self.handle]
        if owned:
            if len(owned) != 1 or owned[0].get('kind') != 'netem' or owned[0].get('parent') != self.parent:
                raise ValueError('Delay ownership changed; refusing to remove another qdisc')
            self.tc('qdisc', 'del', 'dev', 'eth3', 'parent', self.parent, 'handle', self.handle)
        # TBF graft(NULL) installs noop_qdisc, which drops packets. Reapplying
        # the same TBF parameters creates its normal FIFO again, retaining the
        # root handle and the experiment's rate/burst/latency settings.
        self.tc('qdisc', 'change', 'dev', 'eth3', 'root', 'handle', self.root['handle'],
                'tbf', 'rate', '70mbit', 'burst', '32kb', 'latency', '50ms')
        rows = self.rows()
        if any(r.get('handle') == self.handle for r in rows):
            raise ValueError('Delay removal unconfirmed')
        roots = [r for r in rows if r.get('root') is True]
        if (len(roots) != 1 or roots[0].get('kind') != 'tbf'
                or roots[0].get('handle') != self.root['handle']
                or roots[0].get('options') != self.root['options']):
            raise ValueError('Root shaping changed during the delay experiment')
        self.parent = None


def record(path, **entry):
    with path.open('a') as stream:
        stream.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), **entry}, allow_nan=False) + '\n')


def sample(collector, label, deadline, path):
    measured = collector._sample(deadline)
    result = collector._runtime(measured)
    record(path, event='reference_window', label=label, measurement=result.model_dump(mode='json'))
    return result


def setup(lab, namespaces, root, manifest, original, clock):
    """Gate the known split using the same executor and runtime checks as a trial."""
    deadline = Deadline(240)
    traffic = ReceiverTraffic(lab, namespaces.socket_at)
    collector = LiveCollector(lab, namespaces, traffic,
                              lambda c: fingerprint(normalize_readback(c)), 'latency-recovery', clock, 6)
    schedule = load_schedule(json.dumps(workload(360)).encode(), manifest)[0]
    heavy = next(p for p in reference_profiles(schedule).values() if p)
    events = root / 'setup-events.jsonl'
    try:
        for flow, rate in heavy:
            traffic.start(flow, rate, timeout_seconds=deadline.remaining())
        healthy = sample(collector, 'original', deadline, events)
        collector.baselines[healthy.demand_id] = healthy
        frame = collector(None, deadline)
        executor = VerifiedExecutor(lab, ProcessVerifier([sys.executable, '-m', 'advnet.batfish_gate']),
                                    collector.window, collector.scope, healthy_configs=original,
                                    normalize=normalize_readback, congestion_threshold=.85)
        plan = executor.prepare(proposal('sva', 'Ethernet2', 31), frame.model_input, deadline)
        folder = root / 'setup'
        folder.mkdir()
        atomic_json(folder / 'checkpoint.json', original)
        result = executor.execute(
            plan, 'optimization', 'live', folder, deadline, lambda: Deadline(120), clock.now, 15,
            lambda phase, **fields: record(events, event='setup_phase', phase=phase, **fields),
            lambda before, after: record(events, event='readback', before=before, after=after))
        if result.status != 'success':
            raise ValueError('Known route split failed setup acceptance: ' + result.evidence)
        return sample(collector, 'accepted_split', deadline, events)
    finally:
        traffic.cleanup(timeout_seconds=5)


def validate(lab, namespaces, root, manifest, reference, delay_ms, clock):
    """Prove that delay is measurable and the reference action can avoid it."""
    traffic = ReceiverTraffic(lab, namespaces.socket_at)
    collector = LiveCollector(lab, namespaces, traffic,
                              lambda c: fingerprint(normalize_readback(c)), 'latency-recovery', clock, 6)
    delay = LinkDelay(lab, namespaces, delay_ms)
    deadline = Deadline(180)
    schedule = load_schedule(json.dumps(workload(360)).encode(), manifest)[0]
    heavy = next(p for p in reference_profiles(schedule).values() if p)
    events = root / 'validation-events.jsonl'
    try:
        for flow, rate in heavy:
            traffic.start(flow, rate, timeout_seconds=deadline.remaining())
        record(events, event='fault_applied', qdiscs=delay.apply())
        impaired = sample(collector, 'delay_present', deadline, events)
        if (not impaired.all_reachable or not impaired.ospf_healthy
                or impaired.performance_check(reference)['accepted']):
            raise ValueError('Fault did not produce isolated performance degradation')
        # Reference action is used only in this validation arm, never as model feedback.
        configs = lab.configs(deadline)
        candidate = candidate_configs(configs, proposal('den', 'Ethernet3', 100))
        verifier = ProcessVerifier([sys.executable, '-m', 'advnet.batfish_gate'])
        verdict = verifier.verify(snapshot(root / 'reference-snapshot', candidate, manifest), deadline)
        atomic_json(root / 'reference-verification.json', verdict)
        if not verified(verdict):
            raise ValueError('Reference repair failed Batfish')
        lab.replace({'den': candidate['den']}, root / 'reference-deploy', deadline)
        if normalize_readback(lab.configs(deadline)) != normalize_readback(candidate):
            raise ValueError('Reference repair readback mismatch')
        if not lab.wait_convergence(deadline, 60, require_healthy=True):
            raise ValueError('Reference repair did not converge')
        for i in range(3):
            repaired = sample(collector, 'reference_repair_' + str(i + 1), deadline, events)
            if not repaired.all_reachable or not repaired.ospf_healthy or not repaired.performance_check(reference)['accepted']:
                raise ValueError('Reference repair did not restore performance')
        return {'fault_rtt_ms': impaired.mean_rtt_ms, 'reference_rtt_ms': reference.mean_rtt_ms,
                'repair_last_rtt_ms': repaired.mean_rtt_ms, 'reference_repair_windows_passed': 3}
    finally:
        try:
            delay.remove()
        finally:
            traffic.cleanup(timeout_seconds=5)


@contextmanager
def fault_session(root, delay_ms, after, hold, owner, collector, namespaces):
    """Schedule a directed delay while the normal controller owns the lab lock."""
    stop, errors = Event(), []
    delay = LinkDelay(collector.lab, namespaces, delay_ms)
    events = root / 'fault-events.jsonl'

    def worker():
        try:
            if stop.wait(after):
                return
            record(events, event='fault_apply_started')
            rows = delay.apply()
            record(events, event='fault_applied', qdiscs=rows)
            print('Injected Denver→Kansas City delay.', flush=True)
            stop.wait(hold)
        except BaseException as error:
            errors.append(type(error).__name__)
            record(events, event='fault_error', error_type=type(error).__name__)
            owner.stop.set()
        finally:
            try:
                record(events, event='fault_remove_started')
                delay.remove()
                record(events, event='fault_removed')
            except BaseException as error:
                errors.append(type(error).__name__)
                owner.stop.set()

    thread = Thread(target=worker, name='controlled-link-delay', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(30)
        if thread.is_alive() or errors:
            raise RuntimeError('Delay injection/cleanup failed; inspect fault-events.jsonl')


def timed_results(root):
    """Keep recovery during a still-active fault separate from fault removal."""
    def read_rows(path):
        return [json.loads(line) for line in path.read_text().splitlines()]
    fault = read_rows(root / 'fault-events.jsonl')
    applied = next(datetime.fromisoformat(e['at']) for e in fault if e['event'] == 'fault_applied')
    removed = next(datetime.fromisoformat(e['at']) for e in fault if e['event'] == 'fault_removed')
    active_end = next((datetime.fromisoformat(e['at']) for e in fault
                       if e['event'] == 'fault_remove_started'), removed)
    folder = root / 'trials' / 'controller'
    rows = read_rows(folder / 'events.jsonl')
    state = json.loads((folder / 'controller.json').read_text())
    permits = [datetime.fromisoformat(e['at']) for e in rows if e['event'] == 'observation'
               and e['decision']['recommendation'] == 'permit_repair_evaluation'
               and 'performance_failure' in e['decision']['reasons']]
    repairs = [datetime.fromisoformat(r['outcomes'][-1]['occurred_at'])
               for r in state['policy']['state']['interventions']
               if r['kind'] == 'repair' and r['outcomes'] and r['outcomes'][-1]['outcome']['status'] == 'success']
    result = {'fault_applied_at': applied.isoformat(), 'fault_removed_at': removed.isoformat(),
              'fault_removal_started_at': active_end.isoformat(),
              'fault_to_performance_permit_seconds': next(((t-applied).total_seconds() for t in permits
                                                          if applied <= t < active_end), None),
              'fault_to_accepted_repair_seconds': next(((t-applied).total_seconds() for t in repairs
                                                       if applied <= t < active_end), None)}
    windows = [e['runtime'] for e in rows if e['event'] == 'observation' and e.get('runtime')
               and not e['runtime'].get('error')]
    for label, accept in [('before_fault', lambda a,b: b < applied),
                          ('during_fault', lambda a,b: a >= applied and b <= active_end),
                          ('after_removal', lambda a,b: a > removed)]:
        selected = [w for w in windows if accept(datetime.fromisoformat(w['start']), datetime.fromisoformat(w['end']))]
        rtts = [w['mean_rtt_ms'] for w in selected if w.get('mean_rtt_ms') is not None]
        result[label] = {'windows': len(selected), 'median_mean_probe_rtt_ms': median(rtts) if rtts else None,
                         'host_or_ospf_failed_windows': sum(not w['all_reachable'] or not w['ospf_healthy'] for w in selected)}
    result['controller_summary'] = [json.loads(f.read_text()) for f in folder.glob('sessions/*/summary.json')]
    return result


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--arm', choices=['validate', 'observe', 'jev', 'llm'], required=True)
    cli.add_argument('--output', type=Path, required=True)
    cli.add_argument('--delay-ms', type=int, default=200, metavar='100..300')
    cli.add_argument('--match', type=Path, help='Require the same protocol and initial lab state as this plan.json')
    args = cli.parse_args(argv)
    if not 100 <= args.delay_ms <= 300:
        cli.error('--delay-ms must be from 100 to 300')
    if args.arm in ('jev', 'llm') and (not os.environ.get('OPEN_ROUTER_KEY') or not os.environ.get('LLM_MODEL')
                                      or (args.arm == 'jev' and not os.environ.get('SYSONE_MODEL'))):
        cli.error('Export OPEN_ROUTER_KEY, LLM_MODEL and, for Jev, SYSONE_MODEL before starting')
    root = args.output.resolve()
    manifest = json.loads((ROOT / 'topologies/abilene.json').read_text())
    lab, clock = Lab(manifest), Clock()
    original = None
    summary = {'arm': args.arm, 'delay_ms': args.delay_ms, 'duration_seconds': 360,
               'fault_after_seconds': 60, 'fault_hold_seconds': 180,
               'scope': 'Known setup split; natural decisions in timed arms',
               'status': 'failed', 'configs_restored': False, 'shaping_unchanged': False,
               'fault_cleared': False}
    plan = {**summary, 'manifest_sha256': fingerprint(manifest),
            'workload_sha256': fingerprint(workload(360)), 'source_sha256': {
                str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT / 'advnet').glob('*.py'))}}
    matching = json.loads(args.match.read_text()) if args.match else None
    if matching is not None:
        for key in ('source_sha256', 'manifest_sha256', 'workload_sha256', 'delay_ms',
                    'duration_seconds', 'fault_after_seconds', 'fault_hold_seconds'):
            if matching.get(key) != plan[key]:
                cli.error('Matched protocol differs: ' + key + '. Prepare a separate comparison.')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    atomic_json(root / 'plan.json', plan)
    atomic_json(root / 'workload.json', workload(360))
    try:
        with _lab_lock(LOCK_PATH):
            original = lab.configs(Deadline(60))
            atomic_json(root / 'checkpoint.json', original)
            with LabNamespaces.open(lab, Deadline(60)) as ns:
                capacities = ns.capacities(lab, Deadline(30))
                plan.update(initial_config_sha256=fingerprint(normalize_readback(original)),
                            capacity_sha256=fingerprint(capacities))
                atomic_json(root / 'plan.json', plan)
                if matching is not None and any(matching.get(key) != plan[key] for key in
                                    ('initial_config_sha256', 'capacity_sha256')):
                    raise ValueError('Initial lab configuration/shaping differs from the matched arm')
                expected = {f'{link[side]}:{link[side + "_interface"]}'
                            for link in manifest['links'] for side in ('a', 'b')}
                if set(capacities) != expected or any(rate != 70 for rate in capacities.values()):
                    raise ValueError('Configure all 28 backbone directions to 70 Mbps first')
                print('Accepting the initial route split through Batfish and runtime checks.', flush=True)
                reference = setup(lab, ns, root, manifest, original, clock)
                if args.arm == 'validate':
                    summary.update(validate(lab, ns, root, manifest, reference, args.delay_ms, clock))
        if args.arm != 'validate':
            command_line = ['run', '--duration', '6m', '--system-one', 'jev' if args.arm == 'jev' else 'off',
                            '--workload', str(root / 'workload.json'), '--output', str(root / 'trials' / 'controller')]
            if args.arm == 'observe':
                command_line.append('--observe-only')
            result = controller.main(command_line, session_hook=lambda owner, collector, ns:
                                     fault_session(root, args.delay_ms, 60, 180, owner, collector, ns))
            if result:
                raise RuntimeError('Controller exited ' + str(result))
            summary.update(timed_results(root))
        summary['status'] = 'completed'
    except BaseException as error:
        summary['error_type'] = type(error).__name__
        raise
    finally:
        try:
            if original is not None:
                with _lab_lock(LOCK_PATH):
                    deadline = Deadline(120)
                    current = lab.configs(deadline)
                    changed = {n: text for n, text in original.items()
                               if normalize_readback({n: text}) != normalize_readback({n: current[n]})}
                    if changed:
                        lab.replace(changed, root / 'restore', deadline)
                    converged = lab.wait_convergence(deadline, 60, require_healthy=True)
                    summary['configs_restored'] = converged and normalize_readback(lab.configs(deadline)) == normalize_readback(original)
                    with LabNamespaces.open(lab, deadline) as ns:
                        summary['shaping_unchanged'] = ns.capacities(lab, deadline) == capacities if 'capacities' in locals() else False
                        summary['fault_cleared'] = not any(r.get('handle') == LinkDelay.handle
                                                          for r in LinkDelay(lab, ns).rows())
        except BaseException as error:
            summary.update(status='cleanup_failed', cleanup_error_type=type(error).__name__)
            raise
        finally:
            if not all(summary[key] for key in ('configs_restored', 'shaping_unchanged', 'fault_cleared')):
                summary['status'] = 'cleanup_failed'
            atomic_json(root / 'summary.json', summary)
            print(json.dumps(summary), flush=True)
    return 0 if all(summary[key] for key in ('configs_restored', 'shaping_unchanged', 'fault_cleared')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
