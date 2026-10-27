"""Fixture-only checks; never starts a real model or reads a business database."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import probe_single_model_direct as probe


def crashing_child(conn, profile, backend):
    os._exit(77)


def fake_cgroup(folder: Path, *, limit: str = '943718400') -> None:
    (folder / 'memory.max').write_text(limit)
    (folder / 'memory.swap.max').write_text('0')
    (folder / 'memory.current').write_text('33554432')
    (folder / 'memory.peak').write_text('33554432')
    (folder / 'memory.events').write_text('max 0\noom 0\noom_kill 0\n')


class DirectProbeTests(unittest.TestCase):
    def test_each_profile_simulated_success_and_bounded_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_cgroup(folder)
            for profile in ('e5', 'aligned'):
                with self.subTest(profile=profile), patch.object(
                    probe, '_host_memory', return_value={'MemAvailable': 1048576000, 'SwapFree': 0}
                ):
                    receipt, code = probe.run_probe(
                        profile, 'simulate', folder, deadline_seconds=10, require_linux=False
                    )
                    self.assertEqual(code, 0, receipt)
                    self.assertEqual(receipt['status'], 'MEASURED_NOT_ACCEPTED')
                    self.assertEqual(set(receipt['timings_seconds']), {
                        'loaded', 'query_cold', 'query_warm',
                        'documents_cold', 'documents_warm',
                    })
                    self.assertEqual(receipt['child_exit_code'], 0)
                    self.assertEqual(receipt['cgroup_limit_bytes'], 943718400)
                    self.assertFalse(receipt['production_memory_accepted'])
                    self.assertNotIn(probe.PUBLIC_QUERY, json.dumps(receipt, ensure_ascii=False))
                    for document in probe.PUBLIC_DOCUMENTS:
                        self.assertNotIn(document, json.dumps(receipt, ensure_ascii=False))

    def test_unbounded_cgroup_rejects_before_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_cgroup(folder, limit='max')
            with patch.object(probe.multiprocessing, 'get_context', side_effect=AssertionError('child started')):
                receipt, code = probe.run_probe(
                    'e5', 'simulate', folder, deadline_seconds=10, require_linux=False
                )
            self.assertEqual(code, 5)
            self.assertEqual(receipt['error_code'], 'bounded_cgroup_required')

    def test_timeout_terminates_child_with_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_cgroup(folder)
            with patch.object(probe, '_host_memory', return_value={'MemAvailable': 1048576000, 'SwapFree': 0}):
                receipt, code = probe.run_probe(
                    'e5', 'simulate', folder, deadline_seconds=0.001, require_linux=False
                )
            self.assertEqual(code, 3, receipt)
            self.assertEqual(receipt['error_code'], 'probe_deadline_exceeded')
            self.assertFalse(receipt['production_memory_accepted'])

    def test_crashed_child_returns_explicit_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_cgroup(folder)
            with patch.object(probe, '_host_memory', return_value={'MemAvailable': 1048576000, 'SwapFree': 0}):
                receipt, code = probe.run_probe(
                    'e5', 'simulate', folder, deadline_seconds=10,
                    require_linux=False, child_target=crashing_child
                )
            self.assertEqual(code, 4, receipt)
            self.assertEqual(receipt['status'], 'CHILD_EXITED')
            self.assertIn(receipt['error_code'], ('child_pipe_closed', 'child_exited_before_receipt'))
            self.assertEqual(receipt['child_exit_code'], 77)

    def test_cli_rejection_is_one_json_line(self):
        command = [sys.executable, str(Path(probe.__file__)), '--profile', 'e5']
        result = subprocess.run(command, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 5)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['error_code'], 'arguments_invalid')
        self.assertEqual(result.stderr, '')

    def test_real_requires_private_network_before_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_cgroup(folder)
            with patch.object(probe.sys, 'platform', 'linux'), patch.object(
                probe, '_private_network', return_value=False
            ), patch.object(
                probe.multiprocessing, 'get_context', side_effect=AssertionError('child started')
            ):
                receipt, code = probe.run_probe(
                    'e5', 'real', folder, deadline_seconds=10, require_linux=True
                )
            self.assertEqual(code, 5)
            self.assertEqual(receipt['error_code'], 'linux_private_network_required')

    def test_private_network_requires_root_supplied_host_namespace(self):
        cases = (
            ({}, 'net:[100]', False),
            ({'NAVREN_HOST_NETNS_ID': 'invalid'}, 'net:[100]', False),
            ({'NAVREN_HOST_NETNS_ID': 'net:[100]'}, 'net:[100]', False),
            ({'NAVREN_HOST_NETNS_ID': 'net:[200]'}, 'invalid', False),
            ({'NAVREN_HOST_NETNS_ID': 'net:[200]'}, 'net:[100]', True),
        )
        for environment, own_namespace, expected in cases:
            with self.subTest(environment=environment, own_namespace=own_namespace), patch.dict(
                os.environ, environment, clear=True
            ), patch.object(probe.os, 'readlink', return_value=own_namespace) as readlink:
                self.assertEqual(probe._private_network(), expected)
                self.assertEqual(readlink.call_count, 1 if environment.get('NAVREN_HOST_NETNS_ID')
                                 and environment['NAVREN_HOST_NETNS_ID'].startswith('net:[') else 0)
                if readlink.call_count:
                    readlink.assert_called_once_with('/proc/self/ns/net')

    def test_private_network_rejects_unreadable_self_namespace(self):
        with patch.dict(os.environ, {'NAVREN_HOST_NETNS_ID': 'net:[200]'}, clear=True), patch.object(
            probe.os, 'readlink', side_effect=PermissionError
        ):
            self.assertFalse(probe._private_network())

    def test_probe_has_no_direct_db_or_network_import(self):
        tree = ast.parse(Path(probe.__file__).read_text())
        imports = [alias.name.split('.')[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module.split('.')[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module]
        self.assertFalse(set(imports) & {'sqlite3', 'socket', 'requests', 'urllib', 'httpx'})


if __name__ == '__main__':
    unittest.main()
