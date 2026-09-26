"""脑机病房设备准入控制后台。"""
from src.admission.service import AdmissionService, ScopeFrozenError
from src.admission.store import Store

__all__ = ["AdmissionService", "ScopeFrozenError", "Store"]
