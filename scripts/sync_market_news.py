#!/usr/bin/env python3
"""兼容旧入口；仅运行真实采集，旧静态池已停用。"""
from refresh_market import main
if __name__ == "__main__":
    raise SystemExit(main())
