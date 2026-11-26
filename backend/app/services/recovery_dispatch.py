"""补采派工单服务的兼容入口。"""
from __future__ import annotations

from app.services.incident_dispatch import (
    IncidentDispatchService,
    incident_dispatch_service,
)


class RecoveryDispatchService(IncidentDispatchService):
    """补采派工单领取、执行、签收核对。"""


recovery_dispatch_service = incident_dispatch_service

__all__ = ["RecoveryDispatchService", "recovery_dispatch_service"]
