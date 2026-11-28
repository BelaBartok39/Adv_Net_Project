"""Publishing independently validated action evidence on a derivative snapshot."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from advnet.designated_validation import DesignatedAction
from advnet.thinking_cache import ThinkingCache, VERSION


class DesignatedTests(unittest.TestCase):
    def setUp(self):
        d = TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.cache = ThinkingCache(self.root/'source.sqlite', 'training-protocol')
        self.guard = dict(version=VERSION, kind='repair', demand='training', slow_pairs=[['a','b']])
        action = dict(description='verified action', proposal=json.loads(
            (Path(__file__).resolve().parents[1]/'examples/mock-proposal.json').read_text()))
        with self.cache.database(write=True) as db:
            for key in ['first','other']:
                db.execute('INSERT INTO entries VALUES (?,?,?,?,?)',
                           (key, self.cache.binding, json.dumps(self.guard), json.dumps(action), 'quarantine'))
                db.execute("INSERT INTO evidence VALUES (?,?,?,'success')", (key, 'train', key))
                db.execute('INSERT INTO validation_contexts VALUES (?,?,?,?)', (key, 'train', key, json.dumps(self.guard)))
        self.selected = DesignatedAction(self.cache.path, 'first')
        self.summary = dict(status='completed', configs_restored=True, fault_cleared=True, shaping_unchanged=True)
        self.receipt = dict(action='first', context=self.guard, stable_rtt_ms=[5,6,5],
                            evidence='verified_readback_and_runtime', changes=action['proposal']['changes'])

    def test_derivative_only_promotes_designated_patch_and_records_origin(self):
        output = self.selected.publish(self.root, self.summary, self.receipt)
        self.assertEqual(sha256(self.cache.path.read_bytes()).hexdigest(), self.selected.sha256)
        rows = ThinkingCache(output['path'], self.cache.binding, 'frozen').summary()
        self.assertEqual([(r['status'], r['independent_successes']) for r in rows], [('active',2),('quarantine',1)])
        with closing(sqlite3.connect(output['path'])) as db:
            self.assertEqual(db.execute('SELECT id,source_sha256 FROM designated_validations').fetchall(),
                             [('first',self.selected.sha256)])
        with self.assertRaises(FileExistsError):
            self.selected.publish(self.root, self.summary, self.receipt)

    def test_changed_patch_context_failed_runtime_or_missing_cleanup_cannot_publish(self):
        for s,r in [(dict(self.summary,fault_cleared=False),self.receipt),
                    (self.summary,dict(self.receipt, evidence='rollback')),
                    (self.summary,dict(self.receipt, stable_rtt_ms=[])),
                    (self.summary,dict(self.receipt, changes=[])),
                    (self.summary,dict(self.receipt, context=dict(self.guard,demand='other')))]:
            with self.assertRaises(ValueError):
                self.selected.publish(self.root,s,r)
            self.assertFalse((self.root/'evaluation.sqlite').exists())
