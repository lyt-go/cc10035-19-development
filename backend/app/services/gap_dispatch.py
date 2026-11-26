"""数据缺口补采派工服务的兼容入口。"""
from __future__ import annotations

from app.services.incident_dispatch import (
    IncidentDispatchService,
    incident_dispatch_service,
)


class GapDispatchService(IncidentDispatchService):
    MODULE = "gap_dispatch"
    ENTITY = "缺口补采派工单"


gap_dispatch_service = incident_dispatch_service

__all__ = ["GapDispatchService", "gap_dispatch_service"]
