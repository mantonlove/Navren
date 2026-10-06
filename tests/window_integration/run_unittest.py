"""保存unittest实际计数；缺测试、跳过和预期失败均不构成完整关卡。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-dir', type=Path, required=True)
    parser.add_argument('--pattern', required=True)
    parser.add_argument('--minimum-tests', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.minimum_tests < 1 or not (args.start_dir / args.pattern).is_file():
        parser.error('必须指定存在的测试文件和正数最低用例数')
    if args.output.exists():
        raise FileExistsError(args.output)
    suite = unittest.defaultTestLoader.discover(str(args.start_dir), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    complete = (result.wasSuccessful() and result.testsRun >= args.minimum_tests
                and not result.skipped and not result.expectedFailures)
    record = {'pattern': args.pattern, 'minimum_tests': args.minimum_tests,
              'tests_run': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'skipped': len(result.skipped),
              'expected_failures': len(result.expectedFailures),
              'unexpected_successes': len(result.unexpectedSuccesses),
              'skipped_cases': [{'id': test.id(), 'reason': reason} for test, reason in result.skipped],
              'complete': complete, 'planned_exit_code': 0 if complete else 1}
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return record['planned_exit_code']


if __name__ == '__main__':
    sys.exit(main())
