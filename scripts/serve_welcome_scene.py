#!/usr/bin/env python3
"""欢迎动画、真实本地登录与受保护同源主页；业务源服务保持运行。"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from welcome_auth import create_welcome_server
from sms_aliyun import load_sms_transport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18768)
    parser.add_argument('--database', type=Path, help='隔离验收用认证数据库；默认使用本机持久认证库')
    args = parser.parse_args()
    instance = create_welcome_server(args.port, args.database, sms_transport=load_sms_transport())
    print(f'Navren 欢迎与本地登录：http://127.0.0.1:{instance.server_port}', flush=True)
    try:
        instance.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        instance.server_close()


if __name__ == '__main__':
    main()
