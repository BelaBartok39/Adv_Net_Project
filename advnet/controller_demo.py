"""Deterministic synthetic persistent-controller demonstration. No external services."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .contracts import DeviceState, Observation
from .controller_execution import RuntimeWindow, VerifiedExecutor, fingerprint
from .controller_telemetry import CapacityEvidence, monitor_frame
from .persistent_controller import EngineProvider, Journal, PersistentController, Settings, _infer
from .providers import MockJev, MockLLM
from .stability_policy import HealthMeasurement, PolicyConfig, StabilityPolicy


class DemoClock:
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __init__(self):
        self.seconds = 0.0

    def now(self):
        return self.epoch + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def wait(self, stop, seconds):
        self.seconds += seconds


class DemoNetwork:
    """Small in-memory configuration transport, verifier and passive measurement source."""
    scope = 'synthetic-two-router-network'

    def __init__(self, clock):
        self.clock = clock
        self.manifest = {'routers': {n: {} for n in ('r1', 'r2')}, 'hosts': {}}
        self.initial = {n: 'interface Ethernet1\n   ip ospf cost 10\n' for n in self.manifest['routers']}
        self.config = self.initial.copy()
        self.writes = []
        self.load = lambda t: 0.96 if t == 3 or any(
            start <= t < start+7 for start in (10, 30, 50, 70, 90)) else 0.35
        self.behavior = lambda t: ('success' if t < 30 else 'reject' if t < 50 else
                                   'rollback' if t < 70 else 'unsafe')

    def configs(self, deadline):
        deadline.remaining()
        return self.config.copy()

    def replace(self, configs, directory, deadline):
        deadline.remaining()
        self.writes.append(directory.name)
        if directory.name == 'rollback' and self.behavior(self.clock.seconds) == 'unsafe':
            raise TimeoutError('Synthetic rollback failure')
        self.config.update(configs)

    def wait_convergence(self, deadline, seconds, *, require_healthy):
        deadline.remaining()
        return require_healthy

    def verify(self, path, deadline):
        deadline.remaining()
        assert (path/'intent.json').is_file() and (path/'configs/r1.cfg').is_file()
        return dict(accepted=path.name == 'healthy-snapshot' or self.behavior(self.clock.seconds) != 'reject', details=['Synthetic gate'])

    def sample(self, generation, deadline):
        deadline.remaining()
        at = self.clock.now()
        t = int(self.clock.seconds)
        usage = self.load(t)
        raw = dict(source='synthetic', sampled_at=at.isoformat(), links=[dict(
            a='r1', a_interface='Ethernet1', a_mbps=usage*100, b='r2', b_interface='Ethernet1', b_mbps=20)])
        cap = CapacityEvidence(mbps=100, valid_from=self.clock.epoch-timedelta(days=1),
                               valid_until=self.clock.epoch+timedelta(days=1), evidence='Synthetic measured capacity')
        def health(scope):
            return HealthMeasurement(observed_at=at, measurement_generation=generation, healthy=True,
                                     scope_fingerprint=scope, evidence='Synthetic scoped check')
        model = Observation(scenario_id=f'sample-{t}', observed_at=at, objective='Reduce congestion',
                            requirements=['Maintain connectivity and OSPF'], topology=['r1:Ethernet1 -- r2:Ethernet1'],
                            devices={n: DeviceState(interfaces=['Ethernet1'], running_config=c)
                                     for n, c in self.config.items()}, measurements={'utilization': usage})
        return monitor_frame(raw, observation_id=model.scenario_id, generation=generation,
                             capacities={n+':Ethernet1': cap for n in self.config}, rate_window_start=at-timedelta(seconds=1),
                             reachability=health('all required host pairs'), ospf=health('r1-r2 FULL'), model_input=model)

    def window(self, phase, kind, deadline):
        deadline.remaining()
        end = self.clock.now()
        if phase == 'healthy':
            end = self.clock.epoch-timedelta(minutes=1)
        elif phase == 'after':
            self.clock.seconds += 0.25
            end = self.clock.now()
        bad = phase == 'after' and self.behavior(self.clock.seconds) in ('rollback', 'unsafe')
        return RuntimeWindow(source='synthetic', scope=self.scope, demand_id='synthetic-steady-A', demand_epoch='unchanged',
                             start=end-timedelta(seconds=0.25), end=end,
                             config_sha256=fingerprint(self.initial if phase == 'healthy' else self.config),
                             all_reachable=True, ospf_healthy=True, mean_rtt_ms=1, traffic_scope='single_flow',
                             traffic=dict(received_mbps=10, loss_fraction=0,
                                          max_link_utilization=0.95 if phase == 'before' or bad else 0.45))


class DemoDecision:
    def __call__(self, mode, observation, feedback, attempt):
        cost = 20 + int(observation.scenario_id.split('-')[1])
        proposal = dict(outcome='candidate', diagnosis='Synthetic congestion', evidence=['Synthetic counters'],
                        expected_effect='Test alternate cost', changes=[dict(device='r1', interface='Ethernet1',
                                                                            action='set_ospf_cost', value=cost)])
        result = EngineProvider(MockLLM(proposal), MockJev())(mode, observation, feedback, attempt)
        result['elapsed_ms'] = 0.0  # Synthetic clock; avoid nondeterministic wall-time in the demo.
        return result


class ScheduledReply:
    """Deliver the synthetic provider result on the second monitoring tick."""
    def __init__(self, provider, args, directory, timeout):
        self.provider, self.args, self.path = provider, args, directory/'decision.json'
        self.ticks = 0

    def poll(self):
        self.ticks += 1
        if self.ticks < 2:
            return None
        _infer(self.provider, self.args, self.path)
        return json.loads(self.path.read_text())

    def close(self):
        pass


def demo_controller(directory):
    clock = DemoClock()
    network = DemoNetwork(clock)
    ids = iter(range(1, 1000))
    policy = StabilityPolicy(PolicyConfig(congestion_seconds=2, recovery_seconds=1,
        success_cooldown=8, rejected_cooldown=4, rollback_cooldown=6, min_request_interval=2,
        optimization_retry_seconds=4, max_interventions=10), clock=clock.now,
        id_factory=lambda: f'op-{next(ids)}')
    controller = PersistentController(Settings(mode='jev_llm', source='synthetic', interval=1, duration=100),
        network.sample, DemoDecision(), VerifiedExecutor(network, network, network.window, network.scope, healthy_configs=network.initial),
        Journal(directory), clock=clock, policy=policy, worker=ScheduledReply,
        id_factory=lambda: f'intervention-{next(ids)}')
    return controller, network


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    directory = parser.parse_args().output
    directory.mkdir(parents=True, exist_ok=False)
    controller, _ = demo_controller(directory)
    controller.run()
    records = controller.policy.state.interventions
    outcomes = [r.outcomes[-1].outcome.status for r in records]
    if outcomes != ['success', 'rejected', 'rolled_back', 'rollback_unconfirmed']:
        raise AssertionError(outcomes)
    print(json.dumps(dict(source='synthetic', outcomes=outcomes, held=controller.policy.state.active.intervention_id,
                          events=str(directory/'events.jsonl'))))


if __name__ == '__main__':
    main()
