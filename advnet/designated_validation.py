"""Independent, model-free validation of one quarantined Abilene repair."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time

from .contracts import CatalogAction
from .controller_execution import VerifiedExecutor
from .eos import normalize_readback
from .lab import Deadline
from .persistent_controller import atomic_json
from .stability_policy import ConfigurationChange, PolicyConfig, StabilityPolicy
from .thinking_cache import context, supported_match


class DesignatedAction:
    """Keep the training snapshot read-only and publish a derivative after cleanup."""

    def __init__(self, path, identifier):
        self.path = Path(path).resolve(strict=True)
        self.sha256 = sha256(self.path.read_bytes()).hexdigest()
        self.identifier = identifier
        with closing(self.reader()) as db:
            row = db.execute('SELECT binding,context,action,status FROM entries WHERE id=?',
                             (identifier,)).fetchone()
            if not row or row[3] != 'quarantine':
                raise ValueError('Select a quarantined entry in the training snapshot')
            self.binding, guard, action, _ = row
            self.guard = json.loads(guard)
            self.action = CatalogAction.model_validate_json(action)
            if not db.execute("SELECT 1 FROM evidence WHERE id=? AND outcome='success'", (identifier,)).fetchone():
                raise ValueError('Designated entry lacks prior successful training evidence')

    def reader(self):
        return sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)

    def check(self, guard):
        with closing(self.reader()) as db:
            if not supported_match(db, self.identifier, self.guard, guard):
                raise ValueError('Live context differs from the preselected repair')

    def publish(self, root, summary, receipt):
        if (summary.get('status') != 'completed' or
                not all(summary.get(k) is True for k in ('configs_restored', 'shaping_unchanged', 'fault_cleared')) or
                receipt['evidence'] != 'verified_readback_and_runtime' or
                len(receipt['stable_rtt_ms']) != 3 or receipt['action'] != self.identifier or
                receipt['changes'] != [c.model_dump() for c in self.action.proposal.changes]):
            raise ValueError('Incomplete designated validation or cleanup')
        self.check(receipt['context'])
        if sha256(self.path.read_bytes()).hexdigest() != self.sha256:
            raise ValueError('Training snapshot changed')
        # Use a private file until evidence, origin and entry status commit together.
        dest = root / 'evaluation.sqlite'
        tmp = root / '.pending.sqlite'
        tmp.touch(exist_ok=False)
        try:
            with closing(self.reader()) as src, closing(sqlite3.connect(tmp)) as db:
                src.backup(db)
                with db:
                    key = (self.identifier, str(root.resolve()), 'designated-validation')
                    if db.execute('SELECT 1 FROM evidence WHERE trial=?', (key[1],)).fetchone():
                        raise ValueError('Validation must be a distinct trial')
                    db.execute('CREATE TABLE IF NOT EXISTS designated_validations '
                               '(id TEXT, trial TEXT, intervention TEXT, source_sha256 TEXT, '
                               'PRIMARY KEY(id,trial,intervention))')
                    db.execute('INSERT INTO designated_validations VALUES (?,?,?,?)', (*key, self.sha256))
                    db.execute("INSERT INTO evidence VALUES (?,?,?,'success')", key)
                    db.execute('INSERT INTO validation_contexts VALUES (?,?,?,?)',
                               (*key, json.dumps(receipt['context'], sort_keys=True)))
                    db.execute("UPDATE entries SET status='active' WHERE id=?", (self.identifier,))
            os.link(tmp, dest)  # Refuse to overwrite a previous evaluation snapshot.
        finally:
            tmp.unlink(missing_ok=True)
        return dict(path=str(dest), sha256=sha256(dest.read_bytes()).hexdigest(), action=self.identifier)


def verify_during_delay(selected, collector, lab, verifier, original_split, reference, clock, root, record, deadline):
    """Apply the preselected patch through the normal stability, Batfish and runtime gates."""
    policy = StabilityPolicy(PolicyConfig(), clock=clock.now)
    while True:
        start = clock.monotonic()
        frame = collector(policy.measurement_generation, deadline)
        permit = policy.evaluate(frame.signal)
        record(event='validation_policy', decision=permit.model_dump(mode='json'))
        if permit.recommendation == 'permit_repair_evaluation' and 'performance_failure' in permit.reasons:
            break
        time.sleep(min(deadline.remaining(), max(0, start + 10 - clock.monotonic())))
    observation = frame.model_input
    observation.measurements['stability_policy'] = dict(kind='repair', links=permit.links)
    guard = context(observation)
    selected.check(guard)
    executor = VerifiedExecutor(lab, verifier, collector.window, collector.scope, healthy_configs=original_split,
                                normalize=normalize_readback, current_epoch=lambda: collector.traffic.current()[0])
    plan = executor.prepare(selected.action.proposal, observation, deadline)
    reservation = policy.start_intervention('designated-validation', plan.action, 'repair',
                                            opportunity_id=permit.opportunity_id)
    if reservation.stage != 'reservation':
        raise ValueError('Designated repair did not obtain a policy reservation')
    folder = root / 'designated-validation'
    folder.mkdir()
    atomic_json(folder/'checkpoint.json', plan.before)
    def changed(before, after):
        policy.record_configuration_change(ConfigurationChange(
            occurred_at=clock.now(), scope=executor.scope, before_state=before, after_state=after,
            origin='controller', evidence='Designated validation config readback'))
        record(event='readback', before=before, after=after)

    outcome = executor.execute(plan, 'repair', 'live', folder, deadline, lambda: Deadline(120),
                               clock.now, policy.config.max_age_seconds,
                               lambda phase, **fields: record(event='validation_phase', phase=phase, **fields),
                               changed)
    policy.complete_intervention('designated-validation', outcome)
    if outcome.status != 'success' or outcome.evidence != 'verified_readback_and_runtime':
        raise ValueError('Designated repair failed: ' + outcome.evidence)
    stable = []
    for _ in range(3):
        w = collector.window('after', 'repair', deadline)
        record(event='validation_stability', window=w.summary())
        if (w.config_sha256 != plan.action.after_state or w.demand_epoch != plan.demand_epoch
                or not w.all_reachable or not w.ospf_healthy or not w.performance_check(reference)['accepted']):
            raise ValueError('Post-repair stability check failed while delay was active')
        stable.append(w.mean_rtt_ms)
    deadline.remaining()
    return dict(action=selected.identifier, changes=[c.model_dump() for c in plan.proposal.changes],
                context=guard, evidence=outcome.evidence, stable_rtt_ms=stable, model_calls=0)
