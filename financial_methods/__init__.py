"""无I/O的金融方法模块。服务接线须由基座负责人完成。"""
from .contracts import CONTRACT_VERSION, FinancialMetric, MetricContext, money_metric

__all__ = ["CONTRACT_VERSION", "FinancialMetric", "MetricContext", "money_metric"]
