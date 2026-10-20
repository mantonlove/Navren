#!/usr/bin/env python3
"""显式选择最新复杂分流候选路径；默认off，不读取HTTP提供器配置。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--agent-route-provider', choices=('off','native','jev'), default='off')
    p.add_argument('--port', type=int, default=18768)
    args = p.parse_args()
    server = create_server(port=args.port, agent_route_provider=args.agent_route_provider)
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
