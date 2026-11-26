"""Single-owner continuous observation and verified intervention orchestration."""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import multiprocessing
import os
import re
import signal
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .contracts import Contract, Feedback, Observation, Proposal
from .controller_execution import EvidenceFailure, fingerprint, outcome
from .metrics import inference_metrics
from .pipeline import DecisionEngine
from .stability_policy import ConfigurationChange, StabilityPolicy


class Settings(Contract):
    mode: Literal['llm_only', 'jev_llm']
    source: Literal['synthetic', 'live']
    interval: float = Field(default=5, gt=0)
    duration: float = Field(default=600, gt=0)
    inference_timeout: float = Field(default=45, gt=0)
    operation_timeout: float = Field(default=120, gt=0)
    recovery_timeout: float = Field(default=180, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=5)


class Clock:
    monotonic = staticmethod(time.monotonic)

    def now(self):
        return datetime.now(timezone.utc)

    def wait(self, stop, seconds):
        stop.wait(seconds)


class Budget:
    def __init__(self, clock, end, stop=None):
        self.clock, self.end, self.stop = clock, end, stop

    def remaining(self):
        if self.stop and self.stop.is_set():
            raise InterruptedError('Controller cancelled')
        left = self.end - self.clock.monotonic()
        if left <= 0:
            raise TimeoutError('Controller deadline reached')
        return left


def atomic_json(path, data):
    tmp = None
    try:
        with NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(data, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


class Journal:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root/'controller.json'

    @contextmanager
    def owner(self):
        with (self.root/'.owner').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def load(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        if any(p.name != '.owner' for p in self.root.iterdir()):
            raise ValueError('Missing checkpoint in existing controller directory')
        return None

    def save(self, data):
        atomic_json(self.path, data)

    def record(self, data):
        with (self.root/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(data, allow_nan=False) + '\n')


@dataclass
class EngineProvider:
    """SDK adapters and catalog are injected, initialized in the decision worker."""
    llm: object
    screener: object = None
    catalog: dict | None = None
    threshold: float = 0.9

    def __call__(self, mode, observation, feedback, attempt):
        if mode == 'jev_llm' and self.screener is None:
            raise ValueError('Jev mode requires a screener')
        engine = DecisionEngine(self.screener if mode == 'jev_llm' else None,
                                self.llm, self.catalog or {}, self.threshold)
        return engine.decide(observation, feedback, attempt)


def _infer(provider, args, destination):
    try:
        result = provider(*args)
        raw = result.get('proposal')
        proposal = Proposal.model_validate(raw) if raw else None
        # Keep typed actions and numeric accounting. Free-form provider output
        # may repeat credentials or configuration material from a prompt.
        data = dict(status='decision_complete' if result['status'] == 'decision_complete' else 'decision_error',
                    metrics=inference_metrics(result['calls']),
                    route=result.get('screening', {}).get('effective_route'),
                    elapsed_ms=result.get('elapsed_ms'), proposal=None)
        data['calls'] = []
        for call in result['calls']:
            model = call.get('model')
            data['calls'].append(dict(stage=call['stage'], elapsed_ms=call.get('elapsed_ms'),
                                      model=model if isinstance(model, str) and re.fullmatch(r'[\w./:+-]{1,128}', model)
                                      else None))
        data['threshold'] = result.get('threshold')
        data['selected_probability'] = result.get('screening', {}).get('selected_probability')
        if data['route'] not in ('no_change', 'escalate'):
            data['route'] = 'catalog' if data['route'] else None
        if proposal:
            data['proposal'] = dict(outcome=proposal.outcome, diagnosis='Validated decision',
                                    evidence=['Scoped decision observation'], expected_effect='Requires runtime checks',
                                    changes=[c.model_dump() for c in proposal.changes],
                                    observation_requests=['Refresh telemetry'] if proposal.observation_requests else [])
    except Exception as error:
        data = dict(status='decision_error', error_type=type(error).__name__)
    atomic_json(destination, data)


def _bounded_infer(provider, args, destination, expires):
    remaining = expires - time.monotonic()
    if remaining > 0:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        _infer(provider, args, destination)


class InferenceJob:
    """One killable spawn process. Only the owner process can access the executor."""
    def __init__(self, provider, args, directory, timeout):
        self.result = directory/'decision.json'
        self.process = multiprocessing.get_context('spawn').Process(
            target=_bounded_infer, args=(provider, args, self.result, time.monotonic()+timeout), daemon=True)
        self.process.start()

    def poll(self):
        if self.process.is_alive():
            return None
        if self.process.exitcode != 0:
            raise RuntimeError('Inference worker exited')
        return json.loads(self.result.read_text())

    def close(self):
        if self.process.is_alive():
            self.process.kill()
        self.process.join(2)
        if self.process.is_alive():
            raise RuntimeError('Inference worker did not exit')
        self.process.close()


@dataclass
class Pending:
    token: str
    identifier: str
    kind: str
    observation: Observation
    started: float
    end: float
    job: object
    root: Path


class PersistentController:
    """One run() owner. Providers are bounded callables, storage and clock injectable."""
    def __init__(self, settings, telemetry, decision, executor, journal, *, policy=None,
                 clock=None, cancel=None, recorder=None, worker=InferenceJob, id_factory=None):
        self.settings, self.telemetry, self.decision = settings, telemetry, decision
        self.executor, self.journal, self.worker = executor, journal, worker
        self.clock = clock or Clock()
        self.stop = cancel if cancel is not None else Event()
        self.policy = policy or StabilityPolicy(clock=self.clock.now)
        self.record = recorder or journal.record
        self.id_factory = id_factory or (lambda: uuid4().hex)
        self.pending = None
        self.phase = 'stopped'
        self.episode = None
        self.mutation_started = False

    @property
    def binding(self):
        return [self.executor.scope, self.settings.mode, self.settings.source]

    def event(self, name, **fields):
        try:
            self.record(dict(version=1, at=self.clock.now().isoformat(), mode=self.settings.mode,
                             source=self.settings.source, phase=self.phase, event=name, **fields))
        except Exception as error:
            raise EvidenceFailure('Event recording failed') from error

    def save(self):
        try:
            self.journal.save(dict(version=1, binding=self.binding, phase=self.phase, episode=self.episode, mutation_started=self.mutation_started,
                                   policy=self.policy.snapshot().model_dump(mode='json')))
        except Exception as error:
            raise EvidenceFailure('Controller checkpoint failed') from error

    def transition(self, phase, **fields):
        self.phase = phase
        self.save()
        self.event('transition', **fields)

    def budget(self, recovery=False):
        seconds = self.settings.recovery_timeout if recovery else self.settings.operation_timeout
        end = self.clock.monotonic() + seconds
        return Budget(self.clock, end if recovery else min(end, self.end), None if recovery else self.stop)

    def feedback(self, identifier, stage, reason, *, rollback=None, plan=None):
        if self.episode:
            item = Feedback(attempt=self.episode['attempt'], candidate_id=identifier, stage=stage,
                            accepted=False, details=[reason], rollback_succeeded=rollback,
                            measurements={'patch_sha256': plan.action.action_key,
                                          'changes': [c.model_dump() for c in plan.proposal.changes]} if plan else {})
            self.episode['feedback'].append(item.model_dump())

    def close_token(self, token, reason):
        current = self.policy.state.opportunity
        if current and current.opportunity_id == token:
            decision = self.policy.close_opportunity(token, reason)
            self.event('policy', decision=decision.model_dump(mode='json'))
        self.save()

    def abandon(self, reason):
        pending = self.pending
        pending.job.close()
        self.pending = None
        self.event('inference_discarded', opportunity=pending.token, intervention=pending.identifier,
                   reason=reason, elapsed_seconds=self.clock.monotonic()-pending.started)
        self.close_token(pending.token, 'cancelled')

    def change(self, identifier, before, after):
        self.policy.record_configuration_change(ConfigurationChange(
            occurred_at=self.clock.now(), scope=self.executor.scope, before_state=before, after_state=after,
            evidence='Full configuration readback for ' + identifier))
        self.save()
        self.event('configuration_readback', intervention=identifier, before=before, after=after)

    def recover(self):
        active = self.policy.state.active
        if not active:
            return
        self.transition('recovery', intervention=active.intervention_id)
        record = self.policy.state.interventions[-1]
        try:
            current = fingerprint(self.executor.readback(self.budget()))
        except Exception as error:
            self.transition('blocked', error_type=type(error).__name__)
            return
        if current != record.candidate.before_state:
            self.transition('blocked', intervention=active.intervention_id, readback=current)
            return
        history = [r.change for r in self.policy.state.changes if r.change.scope == self.executor.scope]
        if history and history[-1].after_state != current:
            self.change(active.intervention_id, history[-1].after_state, current)
        status = 'rolled_back' if self.mutation_started or active.status == 'rollback_unconfirmed' else 'failed'
        self.policy.complete_intervention(active.intervention_id, outcome(status, 'restart_checkpoint_readback'))
        self.mutation_started = False
        self.episode = None
        self.transition('cooldown', intervention=active.intervention_id, outcome=status)

    def dispatch(self, decision, frame):
        token = decision.opportunity_id
        kind = self.policy.state.opportunity.kind
        self.transition('evaluation_pending', opportunity=token)
        model = frame.model_input
        if model is None or model.observed_at != frame.signal.observed_at:
            self.close_token(token, 'cancelled')
            self.event('inference_skipped', reason='missing_aligned_model_input', opportunity=token)
            return
        model = Observation.model_validate(model).model_copy(deep=True)
        op = self.policy.state.opportunity
        scope = [(key, getattr(frame.signal, key[7:]).scope_fingerprint if key.startswith('health:') else key)
                 for key in sorted(op.signal_keys)]
        context = {'kind': kind, 'opportunity_id': token, 'links': op.links, 'reasons': op.reasons}
        model.measurements = {**model.measurements, 'stability_policy': context}
        signature = fingerprint([kind, scope, [h.scope_fingerprint if h else None
                                               for h in (frame.signal.reachability, frame.signal.ospf)]])
        if not self.episode or self.episode['signature'] != signature:
            self.episode = dict(signature=signature, keys=op.signal_keys, attempt=0, feedback=[])
        if self.episode['attempt'] >= self.settings.max_attempts:
            self.close_token(token, 'cancelled')
            self.event('inference_skipped', reason='episode_attempt_limit', opportunity=token)
            return
        self.episode['attempt'] += 1
        identifier = self.id_factory()
        directory = self.journal.root/identifier
        directory.mkdir(mode=0o700)
        prior = [Feedback.model_validate(item) for item in self.episode['feedback']]
        args = (self.settings.mode, model, prior, self.episode['attempt'])
        self.transition('decision', opportunity=token, intervention=identifier, attempt=self.episode['attempt'])
        started = self.clock.monotonic()
        timeout = min(self.settings.inference_timeout, self.end-started,
                      (op.expires_at-self.clock.now()).total_seconds())
        if timeout <= 0 or self.stop.is_set():
            self.close_token(token, 'cancelled')
            return
        try:
            job = self.worker(self.decision, args, directory, timeout)
        except Exception as error:
            self.feedback(identifier, 'decision', 'Worker startup failed')
            self.close_token(token, 'rejected')
            self.event('worker_error', error_type=type(error).__name__)
            return
        self.pending = Pending(token, identifier, kind, model, started,
                               started+timeout, job, directory)

    def finish(self, pending, result):
        self.event('decision_result', opportunity=pending.token, intervention=pending.identifier,
                   elapsed_seconds=self.clock.monotonic()-pending.started,
                   status=result['status'], metrics=result.get('metrics'), calls=result.get('calls'), route=result.get('route'),
                   threshold=result.get('threshold'), selected_probability=result.get('selected_probability'),
                   model_ms=result.get('elapsed_ms'), error_type=result.get('error_type'))
        self.transition('proposal_assessment', opportunity=pending.token)
        try:
            if result['status'] != 'decision_complete' or not result.get('proposal'):
                raise ValueError('No candidate')
            proposal = Proposal.model_validate(result['proposal'])
            if proposal.outcome != 'candidate':
                raise ValueError('No candidate')
            self.event('proposal', intervention=pending.identifier, changes=[c.model_dump() for c in proposal.changes])
            limit = self.budget()
            plan = self.executor.prepare(proposal, pending.observation, limit)
            limit.remaining()
        except Exception as error:
            self.event('proposal_rejected', opportunity=pending.token, error_type=type(error).__name__)
            self.feedback(pending.identifier, 'decision', 'No usable scoped candidate with matching checkpoint')
            self.close_token(pending.token, 'rejected')
            return
        history = [r.change for r in self.policy.state.changes if r.change.scope == plan.action.scope]
        if history and history[-1].after_state != plan.action.before_state:
            self.change('external-readback', history[-1].after_state, plan.action.before_state)
        assessment = self.policy.assess_proposal(plan.action, pending.kind, opportunity_id=pending.token)
        self.event('policy', decision=assessment.model_dump(mode='json'))
        if 'proposal_eligible' not in assessment.reasons:
            self.feedback(pending.identifier, 'decision', ', '.join(assessment.reasons))
            self.close_token(pending.token, 'rejected')
            return
        if any(f['measurements'].get('patch_sha256') == plan.action.action_key for f in self.episode['feedback']):
            self.event('proposal_rejected', opportunity=pending.token, reason='repeated_episode_patch')
            self.close_token(pending.token, 'rejected')
            return
        reserved = self.policy.start_intervention(pending.identifier, plan.action, pending.kind,
                                                  opportunity_id=pending.token)
        self.save()
        self.event('policy', decision=reserved.model_dump(mode='json'))
        if reserved.stage != 'reservation':
            self.close_token(pending.token, 'cancelled')
            return
        atomic_json(pending.root/'checkpoint.json', plan.before)
        start = self.clock.monotonic()
        def phase(name, **fields):
            if name == 'deployment':
                self.mutation_started = True
            self.transition(name, intervention=pending.identifier, opportunity=pending.token, **fields)
        result = self.executor.execute(plan, pending.kind, self.settings.source, pending.root, self.budget(), lambda: self.budget(True),
                                       self.clock.now, self.policy.config.max_age_seconds, phase,
                                       lambda a, b: self.change(pending.identifier, a, b))
        self.policy.complete_intervention(pending.identifier, result)
        self.mutation_started = result.status == 'rollback_unconfirmed'
        if result.status == 'rejected':
            self.feedback(pending.identifier, 'verification', result.evidence, plan=plan)
        elif result.status == 'rolled_back':
            self.feedback(pending.identifier, 'runtime', result.evidence, rollback=True, plan=plan)
        else:
            self.episode = None
        phase('blocked' if self.policy.state.active else 'cooldown', outcome=result.status,
              evidence=result.evidence, elapsed_seconds=self.clock.monotonic()-start)

    def _tick(self):
        frame = None
        try:
            limit = self.budget()
            frame = self.telemetry(self.policy.measurement_generation, limit)
            limit.remaining()
            if frame.source != self.settings.source or any(
                    h is not None and not h.scope_fingerprint for h in (frame.signal.ospf, frame.signal.reachability)):
                raise ValueError('Unscoped health or source mismatch')
        except Exception as error:
            self.event('telemetry_error', error_type=type(error).__name__)
            frame = None
        decision = self.policy.evaluate(frame.signal if frame else {})
        self.save()
        self.event('observation', decision=decision.model_dump(mode='json'),
                   measurement=frame.signal.model_dump(mode='json') if frame else None)
        state = self.policy.state
        if self.episode and (decision.recommendation == 'no_intervention' or (
                'recovery_confirmed' in decision.reasons and all(
                    key in state.signals and not state.signals[key].latched and not state.signals[key].count
                    for key in self.episode['keys']))):
            self.episode = None
        if self.pending:
            current = state.opportunity
            if not current or current.opportunity_id != self.pending.token:
                self.abandon('opportunity_invalidated')
            elif self.clock.monotonic() >= self.pending.end or self.stop.is_set():
                self.abandon('timeout_or_cancellation')
            else:
                try:
                    result = self.pending.job.poll()
                except Exception as error:
                    result = dict(status='decision_error', error_type=type(error).__name__)
                if result is not None:
                    p = self.pending
                    p.job.close()
                    self.pending = None
                    if self.clock.monotonic() < p.end and not self.stop.is_set():
                        self.finish(p, result)
                    else:
                        self.close_token(p.token, 'cancelled')
        if (decision.recommendation.startswith('permit_') and not self.pending and not self.stop.is_set()
                and self.clock.monotonic() < self.end):
            self.dispatch(decision, frame)
        elif not self.pending:
            state = self.policy.state
            idle = 'monitoring'
            if state.active:
                idle = 'blocked'
            elif state.cooldown_until and self.clock.now() < state.cooldown_until:
                idle = 'cooldown'
            if self.phase != idle:
                self.transition(idle)

    def run(self):
        lab_owner = nullcontext()
        if self.settings.source == 'live':
            from .dynamic_udp import LOCK_PATH, _lab_lock
            lab_owner = _lab_lock(LOCK_PATH)
        with self.journal.owner(), lab_owner:
            previous = self.journal.load()
            if previous is not None:
                if previous['version'] != 1 or previous['binding'] != self.binding:
                    raise ValueError('Incompatible controller state')
                self.policy = StabilityPolicy.restore(json.dumps(previous['policy']), clock=self.clock.now)
                self.episode = previous['episode']
                self.mutation_started = previous['mutation_started']
            self.end = self.clock.monotonic() + self.settings.duration
            try:
                self.transition('monitoring')
                if not self.stop.is_set():
                    self.recover()
                while self.clock.monotonic() < self.end and not self.stop.is_set():
                    due = self.clock.monotonic() + self.settings.interval
                    self._tick()
                    wake = min(self.end, due)
                    if self.pending and self.pending.end < wake:
                        self.clock.wait(self.stop, max(0, self.pending.end-self.clock.monotonic()))
                        self.abandon('shutdown' if self.stop.is_set() else 'inference_timeout')
                    self.clock.wait(self.stop, max(0, wake-self.clock.monotonic()))
            except BaseException as error:
                self.event('controller_error', error_type=type(error).__name__)
                raise
            finally:
                if self.pending:
                    self.abandon('shutdown')
                op = self.policy.state.opportunity
                if op is not None:
                    self.close_token(op.opportunity_id, 'cancelled')
                self.transition('stopped', cancelled=self.stop.is_set(),
                                blocked=self.policy.state.active is not None)
