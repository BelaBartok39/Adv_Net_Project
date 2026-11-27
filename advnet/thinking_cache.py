"""Experimental, scoped reuse of verified typed LLM actions.

This is action memory, not model training. The controller is the only writer;
providers only read eligible actions. No raw configurations or model prose persist.
"""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from .contracts import CatalogAction, Proposal
from .eos import normalize_readback


VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def context(observation):
    """Fail closed unless the live collector supplied scoped comparable evidence."""
    m = observation.measurements
    policy, performance = m.get('stability_policy', {}), m.get('performance', {})
    if (m.get('error') or policy.get('kind') not in ('repair', 'optimization')
            or any(type(m.get(k)) is not bool for k in ('all_reachable', 'ospf_healthy'))
            or type(performance.get('healthy')) is not bool
            or not m.get('demand_id') or not m.get('capacity_sha256')
            or not isinstance(m.get('failed_pairs'), list)
            or not isinstance(m.get('mismatched_ospf'), list)
            or not isinstance(m.get('pairs'), list) or not m['pairs']
            or type(m.get('mean_rtt_ms')) not in (int, float)
            or type(performance.get('latency_limit_ms')) not in (int, float)):
        raise ValueError('Incomplete thinking-cache context')
    # Hash exact pre-config, inventory, topology, declared policy and traffic
    # profile; observed failure locations remain explicit. Timing/epoch IDs are
    # deliberately excluded so recurrence across separate runs can match.
    return dict(version=VERSION, kind=policy['kind'],
                config=digest(normalize_readback({n: d.running_config for n, d in observation.devices.items()})),
                inventory=digest({n: sorted(d.interfaces) for n, d in observation.devices.items()}),
                topology=digest(sorted(observation.topology)),
                requirements=digest([observation.objective, sorted(observation.requirements)]),
                demand=m['demand_id'], capacity=m['capacity_sha256'],
                reachable=m['all_reachable'], ospf=m['ospf_healthy'],
                performance=performance['healthy'],
                latency_exceeded=m['mean_rtt_ms'] > performance['latency_limit_ms'],
                metric_scope=digest(m.get('metric_definitions')),
                failed_pairs=sorted(m['failed_pairs']), mismatched_ospf=sorted(m['mismatched_ospf']),
                slow_pairs=sorted([p['source'], p['destination']] for p in m['pairs']
                                  if p.get('rtt_avg_ms') is not None
                                  and p['rtt_avg_ms'] > performance['latency_limit_ms']),
                affected_links=sorted(policy.get('links', [])))


class ThinkingCache:
    """Quarantine -> two independent accepted LLM trials -> active; failure retires.

    `collect` learns without retrieval, `adaptive` learns and retrieves, `frozen`
    retrieves without writes. A trial means an output directory, not an attempt
    or a resumed invocation. No automatic rehabilitation of retired actions.
    """
    def __init__(self, path, binding, mode='collect'):
        if mode not in ('collect', 'adaptive', 'frozen'):
            raise ValueError('Unknown thinking-cache mode')
        self.path, self.binding, self.mode = Path(path).resolve(), binding, mode
        # Controlled experiments can close collection before external fault
        # removal. Retrieval and retirement remain independent of this switch.
        self.collecting = True
        self.frozen_sha256 = None
        if mode == 'frozen':
            self.frozen_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()

    @contextmanager
    def database(self, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=5)
        else:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS entries '
                           '(id TEXT PRIMARY KEY, binding TEXT NOT NULL, context TEXT NOT NULL, '
                           'action TEXT NOT NULL, status TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS evidence '
                           '(id TEXT NOT NULL, trial TEXT NOT NULL, intervention TEXT NOT NULL, '
                           'outcome TEXT NOT NULL, PRIMARY KEY(id, trial, intervention))')
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def eligible(self, observation):
        if self.frozen_sha256 is not None and hashlib.sha256(self.path.read_bytes()).hexdigest() != self.frozen_sha256:
            raise ValueError('Frozen thinking cache changed')
        if self.mode == 'collect' or not self.path.exists():
            return {}
        try:
            key = json.dumps(context(observation), sort_keys=True)
        except (ValueError, TypeError):
            return {}
        with self.database() as db:
            rows = db.execute('SELECT id, action FROM entries WHERE binding=? AND context=? AND status=? '
                              'ORDER BY id', (self.binding, key, 'active')).fetchall()
        result = {}
        for identifier, raw in rows:
            action = CatalogAction.model_validate_json(raw)
            action.proposal.check_scope(observation)
            result[identifier] = action
        return result

    def observe(self, *, observation, proposal, decision, status, reason, runtime,
                trial, intervention, before_id, after_id):
        """Only called after the existing executor and final config readback finish."""
        if self.mode == 'frozen':
            return None
        selected = decision.get('selected_action')
        if selected and selected.startswith('learned_'):
            if status == 'success':
                return None  # Cache hits never provide their own promotion evidence.
            with self.database(write=True) as db:
                changed = db.execute('UPDATE entries SET status=? WHERE id=? AND binding=?',
                                     ('retired', selected, self.binding)).rowcount
                if changed:
                    db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?,?)',
                               (selected, trial, intervention, status))
            return dict(action=selected, status='retired') if changed else None
        if (status != 'success' or reason != 'verified_readback_and_runtime'
                or not self.collecting
                or decision.get('route') != 'escalate'
                or decision.get('metrics', {}).get('llm_calls', 0) < 1
                or not runtime.get('acceptance', {}).get('accepted')):
            return None
        guard = context(observation)
        before, after = runtime.get('before', {}), runtime.get('after', {})
        if (before.get('config_sha256') != before_id or guard['config'] != before_id
                or before.get('source') != 'live' or after.get('source') != 'live'
                or after.get('config_sha256') != after_id
                or after.get('all_reachable') is not True or after.get('ospf_healthy') is not True
                or before.get('demand_id') != guard['demand'] or after.get('demand_id') != guard['demand']
                or before.get('demand_epoch') != after.get('demand_epoch')
                or before.get('capacity_sha256') != guard['capacity']
                or after.get('capacity_sha256') != guard['capacity']):
            return None
        proposal = Proposal.model_validate(proposal)
        proposal.check_scope(observation)
        changes = sorted([c.model_dump(mode='json') for c in proposal.changes],
                         key=lambda c: (c['device'], c['interface'], c['action']))
        if proposal.outcome != 'candidate':
            return None
        # Never promote free-form model instructions into future prompts.
        action = CatalogAction(description='Previously verified typed action for matching configuration, '
                               'demand and observed symptoms. Escalate if its conditions do not fit.',
                               proposal=Proposal(outcome='candidate', diagnosis='Verified LLM action memory',
                                                 evidence=['Controller-owned verification and runtime acceptance'],
                                                 changes=changes, expected_effect='Repeat prior measured improvement; '
                                                 'fresh verification and runtime acceptance remain required.'))
        identifier = 'learned_' + digest([self.binding, guard, changes])
        with self.database(write=True) as db:
            db.execute('INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?)',
                       (identifier, self.binding, json.dumps(guard, sort_keys=True),
                        action.model_dump_json(), 'quarantine'))
            db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?,?)',
                       (identifier, trial, intervention, 'success'))
            count = db.execute('SELECT COUNT(DISTINCT trial) FROM evidence WHERE id=? AND outcome=?',
                               (identifier, 'success')).fetchone()[0]
            if count >= 2:
                db.execute('UPDATE entries SET status=? WHERE id=? AND status=?',
                           ('active', identifier, 'quarantine'))
            state = db.execute('SELECT status FROM entries WHERE id=?', (identifier,)).fetchone()[0]
        return dict(action=identifier, status=state, independent_successes=count)

    def summary(self):
        if not self.path.exists():
            return []
        with self.database() as db:
            return [dict(action=r[0], status=r[1], independent_successes=r[2]) for r in db.execute(
                'SELECT e.id,e.status,COUNT(DISTINCT CASE WHEN v.outcome="success" THEN v.trial END) '
                'FROM entries e LEFT JOIN evidence v ON e.id=v.id WHERE e.binding=? GROUP BY e.id '
                'ORDER BY e.id', (self.binding,))]


def main(argv=None):
    """Inspect metadata or create a consistent frozen evaluation snapshot."""
    import argparse
    cli = argparse.ArgumentParser(description=main.__doc__)
    sub = cli.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('path', type=Path)
    freeze = sub.add_parser('freeze')
    freeze.add_argument('source', type=Path)
    freeze.add_argument('destination', type=Path)
    args = cli.parse_args(argv)
    source = (args.path if args.command == 'inspect' else args.source).resolve()
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        if args.command == 'inspect':
            rows = [dict(binding=r[0], action=r[1], status=r[2], independent_successes=r[3])
                    for r in db.execute('SELECT e.binding,e.id,e.status,COUNT(DISTINCT CASE '
                                        'WHEN v.outcome="success" THEN v.trial END) '
                                        'FROM entries e LEFT JOIN evidence v ON e.id=v.id GROUP BY e.id ORDER BY e.id')]
            print(json.dumps(rows, indent=2))
        else:
            args.destination.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation avoids replacing a previous evaluation snapshot.
            with args.destination.open('xb'):
                pass
            try:
                with closing(sqlite3.connect(args.destination)) as output:
                    db.backup(output)
            except BaseException:
                args.destination.unlink()
                raise
            print(json.dumps(dict(output=str(args.destination),
                                  sha256=hashlib.sha256(args.destination.read_bytes()).hexdigest())))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
