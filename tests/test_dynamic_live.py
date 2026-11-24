"""Explicit opt-in smoke test on Nick's already-running Abilene lab."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from uuid import uuid4

from advnet.dynamic_traffic import resolve_file


ROOT = Path(__file__).resolve().parents[1]


def _run_cli(command, directory):
    """Retain logs and request normal runner cleanup before any forced CLI kill."""
    with (directory/'stdout.txt').open('w') as stdout, (directory/'stderr.txt').open('w') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
        try:
            return process.wait(timeout=180)
        except BaseException as error:
            try:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            except Exception as cleanup_error:
                error.add_note('Live CLI shutdown failed: '+str(cleanup_error))
            raise


@unittest.skipUnless(os.environ.get('RUN_DYNAMIC_TRAFFIC_INTEGRATION') == '1',
                     'Set RUN_DYNAMIC_TRAFFIC_INTEGRATION=1 only on the already-running Abilene lab')
class LiveDynamicTrafficTests(unittest.TestCase):
    def test_replay_cli_and_owned_client_cleanup_on_running_abilene(self):
        """Control orchestration only; receiver measurements may be unavailable."""
        manifest_path = ROOT/'topologies/abilene.json'
        manifest = json.loads(manifest_path.read_text())
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
        directory = Path(os.environ.get('ADVNET_TEST_OUTPUT_DIR', str(ROOT/'runs')))/('dynamic-live-'+stamp)
        directory.mkdir(parents=True, exist_ok=False)
        profile = {'duration_seconds': 50, 'seed': 7311, 'flows': [
            {'flow_id': 'smoke-sea-sva', 'source': 'sea', 'destination': 'sva', 'offered_mbps': 1,
             'start_seconds': 0, 'stop_seconds': 45, 'rate_changes': [{'at_seconds': 15, 'offered_mbps': 3}],
             'bursts': [{'start_seconds': 25, 'stop_seconds': 35, 'offered_mbps': 5}]},
            {'flow_id': 'smoke-den-nyc', 'source': 'den', 'destination': 'nyc', 'offered_mbps': 1,
             'start_seconds': 5, 'stop_seconds': 40}]}
        source, saved = directory/'workload.json', directory/'resolved.json'
        source.write_text(json.dumps(profile, indent=2)+'\n')
        resolve_file(source, manifest_path, saved)
        versions = {}
        for name, argv in {
            'docker': ['docker', '--version'],
            'host_image_id': ['docker', 'image', 'inspect', '--format', '{{.Id}}', 'advnet-host:0.1'],
            'receiver_iperf3': ['docker', 'exec', 'clab-'+manifest['lab']+'-'+manifest['routers']['sva']['host'],
                                 'iperf3', '--version'],
        }.items():
            process = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            versions[name] = {'returncode': process.returncode, 'stdout': process.stdout, 'stderr': process.stderr}
            (directory/'versions.json').write_text(json.dumps(versions, indent=2)+'\n')
            self.assertEqual(process.returncode, 0, f'{name} unavailable; retained evidence in {directory}')
        command = [sys.executable, '-m', 'advnet.dynamic_traffic', 'replay', '--schedule', str(saved),
                   '--manifest', str(manifest_path), '--output', str(directory/'execution'), '--allow-live',
                   '--operation-timeout-seconds', '30', '--cleanup-timeout-seconds', '30', '--poll-interval-seconds', '0.5']
        (directory/'command.json').write_text(json.dumps(command, indent=2)+'\n')
        returncode = _run_cli(command, directory)
        self.assertEqual(returncode, 0, f'CLI failed; retained evidence in {directory}')
        evidence = json.loads((directory/'execution/evidence.json').read_text())
        execution = json.loads((directory/'execution/execution.json').read_text())
        intended = json.loads(saved.read_text())['events']
        self.assertEqual([record['intended'] for record in execution['scheduler']['events']], intended)
        self.assertTrue(all(record['status'] == 'completed' for record in execution['scheduler']['events']))
        self.assertEqual(evidence['status'], 'completed')
        self.assertTrue(evidence['cleanup_succeeded'])
        self.assertEqual(evidence['resolved_schedule_sha256'], hashlib.sha256(saved.read_bytes()).hexdigest())
        self.assertEqual(len(execution['rate_changes']), 3)
        for change in execution['rate_changes']:
            self.assertIsNone(change['traffic_gap_seconds'])
        checks = []
        for segment in execution['segments']:
            self.assertTrue(segment['removed'])
            process = subprocess.run(['docker', 'inspect', segment['container_id']],
                                     capture_output=True, text=True, timeout=10)
            checks.append({'container_id': segment['container_id'], 'returncode': process.returncode,
                           'stdout': process.stdout, 'stderr': process.stderr})
            (directory/'cleanup-checks.json').write_text(json.dumps(checks, indent=2)+'\n')
            self.assertNotEqual(process.returncode, 0)
            self.assertIn('No such', process.stderr)


if __name__ == '__main__':
    unittest.main()
