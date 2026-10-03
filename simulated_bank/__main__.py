"""python -m simulated_bank：运行本机接口或推进模拟日期。"""
import argparse
import json
import os
from pathlib import Path

from .http_api import create_server
from .service import SimulatedBank


def main():
    parser = argparse.ArgumentParser(description='Navren独立模拟银行')
    parser.add_argument('command', choices=['serve','advance-day'], nargs='?',default='serve')
    parser.add_argument('--db', default=os.environ.get('NAVREN_SIM_DB', os.environ.get('QINGZHOU_SIM_DB', str(Path.home()/'Library/Application Support/Qingzhou/simulated-bank.sqlite3'))))
    parser.add_argument('--port', type=int, default=18766)
    parser.add_argument('--workspace')
    parser.add_argument('--days', type=int,default=1)
    args = parser.parse_args()
    bank = SimulatedBank(args.db)
    if args.command=='advance-day':
        if not args.workspace:
            parser.error('advance-day需要--workspace')
        print(json.dumps(bank.advance_days(args.workspace,args.days),ensure_ascii=False))
        return
    server = create_server(bank,args.port)
    print(f'模拟银行已启动：http://127.0.0.1:{server.server_port}；全部账户和业务为模拟。',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    main()
