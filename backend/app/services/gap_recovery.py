"""数据缺口补采/签收服务的兼容入口。"""
from __future__ import annotations

from app.services.incident_dispatch import (
    IncidentDispatchService,
    incident_dispatch_service,
)


class GapRecoveryService(IncidentDispatchService):
    MODULE = "gap_recovery"
    ENTITY = "缺口补采申请"


gap_recovery_service = incident_dispatch_service

__all__ = ["GapRecoveryService", "gap_recovery_service"]
