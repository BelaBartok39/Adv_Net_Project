"""Run the live Abilene controller for a bounded observation period."""
import argparse
from collections import Counter
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sys
from threading import Event, Thread
from uuid import uuid4

from .catalog import catalog_hash, experiment_catalog
from .controller_execution import RuntimeWindow, VerifiedExecutor, fingerprint, verified
from .controller_live import LabNamespaces, LiveCollector
from .controller_traffic import ReceiverTraffic, reference_profiles
from .dynamic_scheduler import run_schedule
from .dynamic_traffic import load_schedule
from .eos import normalize_readback
from .lab import Lab
from .persistent_controller import Budget, Clock, EngineProvider, Journal, PersistentController, Settings, atomic_json
from .providers import Jev, LLM
from .stability_policy import PolicyConfig, StabilityPolicy
from .trial import ProcessVerifier, snapshot


def duration(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([smh]?)', value)
    if match:
        unit = {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]]
        if 0 < (seconds := float(match[1]) * unit) <= 86400:
            return seconds
    raise argparse.ArgumentTypeError('Give a positive duration up to 24h, such as 20m or 1200s')


@contextmanager
def cancellation(stop):
    previous = {sig: signal.signal(sig, lambda *_: stop.set())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def live_session(args, controller, lab, schedule, manifest, parameters, summary):
    root, stop, clock = controller.journal.root, controller.stop, controller.clock
    saved = root / 'live-baseline.json'
    resumed = saved.is_file()
    if args.resume != resumed:
        raise ValueError('Use a new output directory or explicitly resume a saved live run')
    startup = Budget(clock, clock.monotonic() + args.setup_timeout, stop)
    profiles = reference_profiles(schedule)
    with LabNamespaces.open(lab, startup) as namespaces:
        traffic = ReceiverTraffic(lab, namespaces.socket_at)
        executor = controller.executor
        collector = LiveCollector(lab, namespaces, traffic, executor.config_id,
                                  executor.scope, clock, args.window_seconds)
        if resumed:
            baseline = json.loads(saved.read_text())
            if baseline['parameters'] != parameters:
                raise ValueError('Live resume requires the original manifest, workload, and controller settings')
            executor.healthy_configs = baseline['configs']
            collector.baselines = {key: RuntimeWindow.model_validate_json(json.dumps(value))
                                   for key, value in baseline['windows'].items()}
        else:
            if not args.observe_only:
                executor.healthy_configs = executor.readback(startup)
                healthy = snapshot(root / 'preflight' / 'snapshot', executor.healthy_configs, manifest)
                verdict = executor.verifier.verify(healthy, startup)
                atomic_json(root / 'preflight' / 'verification.json', verdict)
                if not verified(verdict):
                    raise ValueError('Healthy lab configuration did not pass Batfish preflight')
                collector.calibrate(profiles, executor.config_id(executor.healthy_configs), startup)
            atomic_json(saved, {'parameters': parameters, 'configs': executor.healthy_configs,
                                'windows': {key: window.model_dump(mode='json')
                                            for key, window in collector.baselines.items()}})
            atomic_json(root / 'manifest.json', manifest)
            if schedule is not None:
                atomic_json(root / 'resolved-workload.json', schedule.model_dump(mode='json'))
        startup.remaining()
        controller.telemetry, executor.windows = collector, collector.window
        executor.current_epoch = lambda: traffic.current()[0]
        session_id = uuid4().hex
        artifacts = root / 'sessions' / session_id
        artifacts.mkdir(mode=0o700, parents=True)
        traffic_stop = Event()
        schedule_result = []
        epoch_ready = Event()

        def run_traffic():
            def on_start(origin):
                atomic_json(artifacts / 'traffic-origin.json',
                            {'started_monotonic_seconds': origin, 'recorded_at': clock.now().isoformat()})
                epoch_ready.set()

            try:
                report = run_schedule(schedule, manifest, traffic, cancelled=traffic_stop.is_set,
                                      on_start=on_start)
                schedule_result.append(report)
                atomic_json(artifacts / 'traffic-execution.json', report.model_dump(mode='json'))
                if not report.cleanup_succeeded or report.status not in ('completed', 'cancelled'):
                    stop.set()
            except Exception as error:
                schedule_result.append(error)
                stop.set()
            finally:
                epoch_ready.set()

        worker = None
        try:
            if schedule is not None:
                worker = Thread(target=run_traffic, name='workload-scheduler', daemon=True)
                worker.start()
                if not epoch_ready.wait(startup.remaining()):
                    raise TimeoutError('Workload scheduler did not announce its start')
            controller.event('live_session_started', session=session_id, duration_seconds=args.duration,
                             observe_only=args.observe_only, workload=schedule is not None)
            print(f'Monitoring Abilene for {args.duration:g}s. Evidence in {root}', file=sys.stderr, flush=True)
            yield
        finally:
            traffic_stop.set()
            if worker is not None:
                worker.join(20)
                if worker.is_alive():
                    raise TimeoutError('Workload scheduler did not confirm cleanup')
                report, = schedule_result
                if isinstance(report, BaseException):
                    raise RuntimeError('Workload scheduler failed') from report
                if not report.cleanup_succeeded or report.status not in ('completed', 'cancelled'):
                    raise RuntimeError('Workload failed or did not confirm cleanup')
            else:
                traffic.cleanup(timeout_seconds=5)
            controller.event('live_session_stopped', session=session_id, traffic_cleanup_succeeded=True)
            records = controller.policy.state.interventions
            outcomes = Counter((item.kind, item.outcomes[-1].outcome.status)
                               for item in records if item.outcomes)
            summary.update(session=session_id, cancelled=stop.is_set(),
                           blocked=controller.policy.state.active is not None,
                           observations=collector.observations, receiver_windows=collector.receiver_windows,
                           traffic_cleanup_succeeded=True,
                           outcomes={kind: {status: count for (record_kind, status), count in outcomes.items()
                                            if record_kind == kind} for kind in ('repair', 'optimization')})
            atomic_json(artifacts / 'summary.json', summary)


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('action', choices=['run'])
    cli.add_argument('--duration', type=duration, required=True, help='Observation runtime, e.g. 20m')
    cli.add_argument('--manifest', type=Path, default=Path('topologies/abilene.json'))
    cli.add_argument('--workload', type=Path, help='Dynamic workload definition or saved resolved schedule')
    cli.add_argument('--output', type=Path)
    cli.add_argument('--resume', action='store_true', help='Recover a saved controller journal')
    cli.add_argument('--observe-only', action='store_true', help='Collect evidence without inference or live config writes')
    cli.add_argument('--system-one', choices=['off', 'jev'], default='off')
    cli.add_argument('--model', default=os.environ.get('LLM_MODEL'))
    cli.add_argument('--sysone-model', default=os.environ.get('SYSONE_MODEL'))
    cli.add_argument('--base-url', default=os.environ.get('LLM_BASE_URL'))
    cli.add_argument('--catalog', type=Path)
    cli.add_argument('--interval', type=duration, default=10)
    cli.add_argument('--window-seconds', type=int, default=6)
    cli.add_argument('--setup-timeout', type=duration, default=600)
    cli.add_argument('--inference-timeout', type=duration, default=45)
    args = cli.parse_args(argv)
    if args.resume and args.output is None:
        cli.error('--resume requires --output')
    if not 2 <= args.window_seconds <= 30 or not args.window_seconds <= args.interval <= 300:
        cli.error('--window-seconds must be 2–30 and --interval must cover the window and be at most 5m')
    if args.duration <= args.window_seconds + 1:
        cli.error('--duration must leave time for at least one measurement window')
    if not args.observe_only:
        if not args.model or not os.environ.get('OPEN_ROUTER_KEY'):
            cli.error('Set LLM_MODEL and OPEN_ROUTER_KEY, or use --observe-only')
        if importlib.util.find_spec('openai') is None or importlib.util.find_spec('pybatfish') is None:
            cli.error('Install the openai and batfish extras')
        if args.system_one == 'jev' and (not args.sysone_model or importlib.util.find_spec('typesafe_sdk') is None):
            cli.error('Set SYSONE_MODEL and install the jev extra')
    output = args.output or Path('runs') / ('continuous-' + uuid4().hex[:12])
    try:
        manifest = json.loads(args.manifest.read_text())
        schedule = load_schedule(args.workload.read_bytes(), manifest)[0] if args.workload else None
        catalog = experiment_catalog(manifest, args.catalog)
        lab, clock, stop = Lab(manifest), Clock(), Event()
        settings = Settings(mode='jev_llm' if args.system_one == 'jev' else 'llm_only',
                            source='live', duration=args.duration, interval=args.interval,
                            inference_timeout=args.inference_timeout, observe_only=args.observe_only)
        policy = StabilityPolicy(PolicyConfig(max_gap_seconds=max(15, args.interval * 1.5),
                                              max_age_seconds=max(15, args.window_seconds + 6)),
                                 clock=clock.now)
        parameters = {'manifest_sha256': fingerprint(manifest), 'mode': settings.mode,
                      'observe_only': settings.observe_only, 'model': args.model, 'base_url': args.base_url,
                      'jev_model': args.sysone_model, 'catalog_sha256': catalog_hash(catalog),
                      'window_seconds': args.window_seconds, 'interval': args.interval,
                      'health_probe_workers': LiveCollector.health_probe_workers,
                      'performance_monitor_version': 1,
                      'inference_timeout': args.inference_timeout,
                      'workload_sha256': fingerprint(schedule.model_dump(mode='json')) if schedule else None}
        oracle = ProcessVerifier([sys.executable, '-m', 'advnet.batfish_gate'])
        executor = VerifiedExecutor(lab, oracle, None, manifest['lab'] + ':' + fingerprint(manifest),
                                    healthy_configs={}, normalize=normalize_readback,
                                    congestion_threshold=policy.config.congestion_high)
        llm = LLM('openrouter', args.model, min(60, args.inference_timeout), 4096, args.base_url)
        jev = Jev(args.sysone_model, 10) if args.system_one == 'jev' else None
        controller = PersistentController(settings, None, EngineProvider(llm, jev, catalog), executor,
                                          Journal(output), clock=clock, policy=policy, cancel=stop)
        summary = {'output': str(output), 'mode': settings.mode, 'observe_only': args.observe_only,
                   'duration_seconds': args.duration}
        with cancellation(stop):
            controller.run(session=lambda: live_session(args, controller, lab, schedule, manifest, parameters, summary))
        print(json.dumps(summary))
        if summary['blocked']:
            return 1
        if stop.is_set():
            return 130
        return 0 if summary['observations'] else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Continuous controller: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
