"""轻舟独立模拟银行。金额单位为人民币分，全部业务为模拟。"""
from .service import BankError, SimulatedBank

__all__ = ['BankError', 'SimulatedBank']
