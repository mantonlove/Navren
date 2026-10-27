"""Exercise the account setup decision path with fake system commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parent
SOURCE = ROOT / 'worker_proto' / 'install_worker_account.sh.prototype'

FAKE_COMMAND = r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
path = pathlib.Path(os.environ['FAKE_ACCOUNT_STATE'])
state = json.loads(path.read_text())
def save(): path.write_text(json.dumps(state))
if name == 'id':
    if args == ['-u']: print(0)
    elif args == ['-u','navren-web']: print(999)
    elif args == ['-g','navren-web']: print(988)
    elif args == ['-u','navren-encoder']: print(998)
    elif args == ['-g','navren-encoder']: print(987)
    elif args == ['-G','navren-web']: print(*state['web_groups'])
    elif args == ['-G','navren-encoder']: print(*state['worker_groups'])
    else: raise SystemExit(2)
elif name == 'getent':
    if args == ['passwd','navren-web']:
        print('navren-web:x:999:988::/nonexistent:' + os.environ['FAKE_NOLOGIN'])
    elif args == ['passwd','navren-encoder']:
        print('navren-encoder:x:998:987::/nonexistent:' + os.environ['FAKE_NOLOGIN'])
    elif args == ['group','navren-web']:
        print('navren-web:x:988:')
    elif args == ['group','navren-encoder']:
        print('navren-encoder:x:987:navren-web')
    else: raise SystemExit(2)
elif name == 'usermod':
    if args != ['-aG','navren-encoder','navren-web']:
        raise SystemExit(2)
    state['calls'].append('usermod -aG navren-encoder navren-web')
    state['web_groups'].append(987)
    save()
elif name in ('groupadd','useradd'):
    state['calls'].append(name)
    save()
    raise SystemExit(2)
else:
    raise SystemExit(2)
'''


class AccountSetupTests(unittest.TestCase):
    def fixture(self, *, web_groups=(988,987), worker_groups=(987,)):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        nologin = folder / 'nologin'
        nologin.write_text('#!/bin/sh\nexit 1\n')
        nologin.chmod(0o755)
        script = folder / 'setup.sh'
        script.write_text(SOURCE.read_text().replace('/usr/sbin/nologin',str(nologin)))
        bin_dir = folder / 'bin'
        bin_dir.mkdir()
        command = bin_dir / 'fake-command'
        command.write_text(f'#!{sys.executable}\n' + FAKE_COMMAND)
        command.chmod(0o755)
        for name in ('id','getent','usermod','groupadd','useradd'):
            (bin_dir/name).symlink_to(command)
        state_file = folder / 'state.json'
        state_file.write_text(json.dumps({'web_groups':list(web_groups),
                                          'worker_groups':list(worker_groups),'calls':[]}))
        env = {**os.environ,'PATH':str(bin_dir)+':/usr/bin:/bin',
               'FAKE_ACCOUNT_STATE':str(state_file),'FAKE_NOLOGIN':str(nologin)}
        return script, state_file, env

    @staticmethod
    def run_script(script, env):
        return subprocess.run(['/bin/sh',str(script)],env=env,text=True,
                              capture_output=True,timeout=5)

    def test_existing_is_idempotent_and_preserves_web_groups(self):
        script, state_file, env = self.fixture(web_groups=(988,990,987))
        for _ in range(2):
            result = self.run_script(script,env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'worker_account_verified')
        state = json.loads(state_file.read_text())
        self.assertEqual(state['web_groups'],[988,990,987])
        self.assertEqual(state['calls'],[])

    def test_worker_with_other_supplementary_group_fails_before_mutation(self):
        script, state_file, env = self.fixture(worker_groups=(987,988),web_groups=(988,))
        result = self.run_script(script,env)
        self.assertEqual(result.returncode,1)
        self.assertIn('worker_extra_groups',result.stderr)
        self.assertEqual(json.loads(state_file.read_text())['calls'],[])

    def test_web_group_is_appended_once_without_replacing_other_groups(self):
        script, state_file, env = self.fixture(web_groups=(988,990))
        for _ in range(2):
            result = self.run_script(script,env)
            self.assertEqual(result.returncode,0,result.stderr)
        state = json.loads(state_file.read_text())
        self.assertEqual(state['web_groups'],[988,990,987])
        self.assertEqual(state['calls'],['usermod -aG navren-encoder navren-web'])


if __name__ == '__main__':
    unittest.main()
