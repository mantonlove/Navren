#!/usr/bin/env python3
"""显式选择 Jev 实验路径的 Navren 服务入口；默认关闭优化。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime.jev_agent import JevConversationAgent
from server import create_server

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--mode', choices=['off', 'shadow', 'active'], default='off')
    p.add_argument('--port', type=int, default=18768)
    args = p.parse_args()
    server = create_server(port=args.port, conversation_agent=JevConversationAgent(jev_mode=args.mode))
    try:
        server.serve_forever()
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
