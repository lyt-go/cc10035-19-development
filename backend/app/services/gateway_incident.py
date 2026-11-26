"""网关事故服务的兼容入口。

核心实现位于 incident_dispatch.IncidentDispatchService；这个模块保留按
“网关事故”命名导入的调用方/测试习惯。
"""
from __future__ import annotations

from app.services.incident_dispatch import (
    STATE_COMPLETED_ORIGINAL,
    STATE_NOT_SENT,
    STATE_OUTBOUND_PENDING,
    IncidentDispatchService,
    incident_dispatch_service,
)


class GatewayIncidentService(IncidentDispatchService):
    """按网关事故号查询并派发缺口。"""

    MODULE = "gateway_incident"
    ENTITY = "网关事故"
    LIST_FIELDS = [
        "事故编号", "网关事故号", "网关编号", "事故名称",
        "故障开始", "故障结束", "圈定设备身份", "status",
    ]
    REQUIRED_FIELDS = ["事故编号", "网关编号"]
    STATUS_ORDER = ["待处理", "处理中", "已关闭"]
    ACTION_RULES = {"开始处理": "处理中", "关闭事故": "已关闭"}


gateway_incident_service = incident_dispatch_service

__all__ = [
    "GatewayIncidentService",
    "gateway_incident_service",
    "STATE_NOT_SENT",
    "STATE_OUTBOUND_PENDING",
    "STATE_COMPLETED_ORIGINAL",
]
