#!/usr/bin/env python3
"""Fail-closed launcher for the existing Navren collector; no scheduling policy here."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo
import pwd
import re
import runpy
import stat
import sys


BASE = Path('/opt/navren/market-collector')
CURRENT = BASE / 'current'
APPROVAL = Path('/etc/navren-market/approved-cutover.json')
DATABASE = Path('/var/lib/navren-market/public/market.sqlite3')
WORKING_DIRECTORY = Path('/var/lib/navren-market')
LOG_DIRECTORY = Path('/var/log/navren-market')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class GateError(Exception):
    def __init__(self, code: str):
        self.code = code


def emit(status: str, code: str, **fields: object) -> None:
    print(json.dumps({'status': status, 'code': code, **fields},
                     ensure_ascii=True, sort_keys=True), flush=True)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GateError(code)


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def root_metadata(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode), 'unexpected_symlink')
    require(info.st_uid == 0 and info.st_mode & 0o022 == 0,
            'root_ownership_or_mode_invalid')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            'unexpected_file_type')
    return info


def root_directory_chain(path: Path) -> None:
    require(path.is_absolute(), 'absolute_path_required')
    for directory in reversed([path, *path.parents]):
        root_metadata(directory, directory=True)


def read_root_json(path: Path) -> tuple[dict, str]:
    root_directory_chain(path.parent)
    info = root_metadata(path)
    require(0 < info.st_size <= 2 * 1024 * 1024, 'metadata_size_invalid')
    raw = path.read_bytes()
    require(len(raw) == info.st_size, 'metadata_changed_during_read')
    value = json.loads(raw, object_pairs_hook=unique_object)
    require(isinstance(value, dict), 'metadata_object_required')
    return value, hashlib.sha256(raw).hexdigest()


def pin(value: object) -> str:
    if isinstance(value, dict):
        value = value.get('sha256')
    require(isinstance(value, str) and SHA256.fullmatch(value) is not None,
            'source_pin_invalid')
    return value


def source_path(key: object) -> PurePosixPath:
    require(isinstance(key, str), 'source_path_invalid')
    relative = PurePosixPath(key)
    require(not relative.is_absolute() and relative.parts[0:1] == ('src',)
            and len(relative.parts) > 1 and '..' not in relative.parts
            and not any(part.startswith('._') for part in relative.parts)
            and relative.as_posix() == key, 'source_path_invalid')
    return relative


def verify_package() -> tuple[Path, str, dict[str, str]]:
    root_directory_chain(BASE)
    current_info = CURRENT.lstat()
    require(current_info.st_uid == 0, 'current_owner_invalid')
    if not stat.S_ISLNK(current_info.st_mode):
        root_metadata(CURRENT, directory=True)
    release = CURRENT.resolve(strict=True)
    require(release != BASE and release.is_relative_to(BASE), 'release_path_invalid')
    root_directory_chain(release)
    manifest, manifest_sha = read_root_json(release / 'collector-manifest.json')
    files = manifest.get('files')
    require(isinstance(files, dict) and bool(files), 'manifest_files_missing')
    pins = {}
    for key, value in files.items():
        relative = source_path(key)
        target = release.joinpath(*relative.parts)
        root_directory_chain(target.parent)
        root_metadata(target)
        expected = pin(value)
        require(hashlib.sha256(target.read_bytes()).hexdigest() == expected,
                'source_hash_mismatch')
        pins[key] = expected
    source = release / 'src'
    root_metadata(source, directory=True)
    actual = set()
    for target in source.rglob('*'):
        if any(part.startswith('._') for part in target.relative_to(source).parts):
            continue  # AppleDouble metadata is never a package/import member.
        info = target.lstat()
        require(not stat.S_ISLNK(info.st_mode), 'source_symlink_denied')
        if stat.S_ISDIR(info.st_mode):
            root_metadata(target, directory=True)
        else:
            root_metadata(target)
            actual.add(target.relative_to(release).as_posix())
    require(actual == set(pins), 'source_inventory_mismatch')
    require('src/scripts/refresh_market.py' in pins, 'collector_entrypoint_missing')
    require('src/market_schedule.py' in pins and 'src/market_pipeline.py' in pins,
            'original_scheduler_missing')
    return source, manifest_sha, pins


def verify_approval(manifest_sha: str, pins: dict[str, str]) -> dict:
    try:
        approved, _ = read_root_json(APPROVAL)
    except FileNotFoundError:
        raise GateError('cutover_not_armed') from None
    require(approved.get('manifest_sha256') == manifest_sha, 'manifest_not_approved')
    require(approved.get('db_path') == str(DATABASE), 'database_not_approved')
    require(approved.get('local_collector_disabled') is True,
            'local_collector_not_disabled')
    require(approved.get('allow_paid_collection') is True, 'paid_collection_not_armed')
    handoff = approved.get('local_handoff_sha256')
    require(isinstance(handoff, str) and SHA256.fullmatch(handoff) is not None,
            'handoff_pin_missing')
    web = approved.get('web_market_contract_sha256')
    require(isinstance(web, dict) and bool(web), 'web_contract_missing')
    required = {key for key in pins if
                (len(PurePosixPath(key).parts) == 2
                 and PurePosixPath(key).name.startswith('market')
                 and key.endswith('.py'))
                or key == 'src/public_market_history.py'}
    require(required.issubset(web), 'web_contract_incomplete')
    for key, value in web.items():
        require(key in pins and pin(value) == pins[key], 'web_contract_mismatch')
    return approved


def verify_data_metadata() -> None:
    # Never open SQLite here: even MarketStore construction changes live state.
    for directory in (WORKING_DIRECTORY, DATABASE.parent, LOG_DIRECTORY):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
                'runtime_directory_invalid')
        require(directory.resolve(strict=True) == directory, 'runtime_symlink_denied')
    info = DATABASE.lstat()
    require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_nlink == 1 and info.st_size > 0, 'database_file_invalid')
    require(DATABASE.resolve(strict=True) == DATABASE, 'database_symlink_denied')


def verify_activation_boundary(approved: dict) -> None:
    # Root's activation boundary is separate from, and never alters, plan().
    value = approved.get('not_before')
    require(isinstance(value, str) and 0 < len(value) <= 80,
            'activation_boundary_missing')
    try:
        boundary = datetime.fromisoformat(value)
        require(boundary.tzinfo is not None and boundary.utcoffset() is not None,
                'activation_timezone_required')
        require(datetime.now(timezone.utc) >= boundary.astimezone(timezone.utc),
                'activation_not_due')
    except (ValueError, TypeError, OverflowError):
        raise GateError('activation_boundary_invalid') from None


def owner_lock() -> int:
    path = DATABASE.parent / 'collector-owner.lock'
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o660)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == os.geteuid() and info.st_mode & 0o007 == 0,
                'owner_lock_invalid')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateError('collector_already_running') from None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def load_only_collector_credential() -> None:
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    require(bool(directory), 'credential_directory_missing')
    os.environ.pop('AIPING_API_KEY', None)
    os.environ.pop('DASHSCOPE_API_KEY', None)
    for name in ('DEEPSEEK_API_KEY', 'GLM_API_KEY'):
        target = Path(directory) / name
        info = target.lstat()
        require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and 0 < info.st_size <= 16384, 'collector_credential_invalid')
        value = target.read_text().strip()
        require(bool(value) and '\n' not in value and '\r' not in value,
                'collector_credential_invalid')
        os.environ[name] = value


def temporary_budget_phase(control: dict, now: datetime, controlled: bool) -> str:
    if controlled:
        return 'controlled_recovery'
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    if (local.date().isoformat() == control['automatic_date']
            and (local.hour, local.minute) < (12, 30)):
        return 'automatic_morning'
    return 'native_scheduled'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true',
                        help='metadata and hashes only; never import business, open DB or load keys')
    parser.add_argument('--controlled-recovery', action='store_true')
    args = parser.parse_args()
    source, manifest_sha, pins = verify_package()
    verify_approval(manifest_sha, pins)
    verify_data_metadata()
    if args.preflight:
        emit('preflight_passed', 'no_business_or_credential_execution',
             manifest_sha256=manifest_sha, source_files=len(pins), armed=True)
        return 0
    require(os.geteuid() == pwd.getpwnam('navren-market').pw_uid
            and os.geteuid() != 0, 'collector_identity_invalid')
    descriptor = owner_lock()
    try:
        # A separate outer lock covers MarketStore's constructor. Do not take
        # market.sqlite3.lock: original refresh() must acquire its own lock.
        source, current_sha, current_pins = verify_package()
        require(current_sha == manifest_sha, 'package_changed_before_run')
        approved = verify_approval(current_sha, current_pins)
        verify_activation_boundary(approved)
        verify_data_metadata()
        load_only_collector_credential()
        os.environ['NAVREN_MARKET_DB'] = str(DATABASE)
        os.environ['NAVREN_MODEL_CALL_LOG'] = str(LOG_DIRECTORY / 'model-calls.jsonl')
        os.environ['NAVREN_MARKET_MODEL_LOG'] = str(LOG_DIRECTORY / 'market-model-calls.jsonl')
        os.environ['TZ'] = 'Asia/Shanghai'
        os.environ['NAVREN_MARKET_SCHEDULER_MODE'] = 'server_timer'
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        os.environ.pop('PYTHONPATH', None)
        os.environ.pop('PYTHONHOME', None)
        sys.dont_write_bytecode = True
        # Exclude the writable cwd from import resolution; original script
        # inserts its pinned source parent at index zero itself.
        sys.path[:] = [entry for entry in sys.path if entry
                       and not Path(entry).resolve().is_relative_to(WORKING_DIRECTORY)]
        os.chdir(WORKING_DIRECTORY)
        entrypoint = source / 'scripts' / 'refresh_market.py'
        control = Path('/etc/navren-market/official-budget-control.json')
        root_metadata(control)
        raw_control = control.read_bytes()
        control_value = json.loads(raw_control)
        os.environ['NAVREN_MARKET_BUDGET_CONTROL'] = str(control)
        os.environ['NAVREN_MARKET_BUDGET_CONTROL_SHA256'] = hashlib.sha256(raw_control).hexdigest()
        os.environ['NAVREN_MARKET_BUDGET_LEDGER'] = str(LOG_DIRECTORY / 'official-budget-ledger.json')
        require(control_value.get('schema') == 'navren-market-budget-v1', 'official_budget_control_invalid')
        activation = control_value.get('controlled_activation', {})
        if not args.controlled_recovery and activation.get('enable_once') is True:
            now = datetime.now(timezone.utc)
            begins = datetime.fromisoformat(activation['not_before'])
            ends = datetime.fromisoformat(activation['not_after'])
            if begins <= now <= ends:
                claim = LOG_DIRECTORY / ('controlled-' + control_value['controlled_date'] + '.claim')
                try:
                    claim_fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(claim_fd, 'w') as handle:
                        handle.write(json.dumps({'control_sha256': hashlib.sha256(raw_control).hexdigest(),
                                                 'claimed_at': now.isoformat(), 'run_kind': 'controlled_recovery'}))
                        handle.flush(); os.fsync(handle.fileno())
                    args.controlled_recovery = True
        os.environ['NAVREN_MARKET_BUDGET_PHASE'] = temporary_budget_phase(
            control_value, datetime.now(timezone.utc), args.controlled_recovery)
        mode = ['--controlled-recovery', '--hotspots-only'] if args.controlled_recovery else ['--scheduled', '--backfill-weeks', '1']
        report = 'market-controlled-recovery-20261026.json' if args.controlled_recovery else 'market-refresh-latest.json'
        sys.argv = [str(entrypoint), *mode, '--database', str(DATABASE), '--report', str(LOG_DIRECTORY / report)]
        emit('collector_started', 'original_scheduled_entrypoint',
             manifest_sha256=manifest_sha, run_kind='controlled_recovery' if args.controlled_recovery else 'scheduled')
        try:
            runpy.run_path(str(entrypoint), run_name='__main__')
        except SystemExit as ended:
            if ended.code is None:
                return 0
            require(type(ended.code) is int and 0 <= ended.code <= 255,
                    'collector_exit_invalid')
            return ended.code
        return 0
    finally:
        for name in ('AIPING_API_KEY', 'DEEPSEEK_API_KEY', 'GLM_API_KEY'):
            os.environ.pop(name, None)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


if __name__ == '__main__':
    try:
        exit_code = main()
    except GateError as error:
        emit('blocked', error.code)
        exit_code = 3
    except BaseException as error:
        emit('failed', 'collector_launcher_error', error_type=type(error).__name__)
        exit_code = 2
    raise SystemExit(exit_code)
