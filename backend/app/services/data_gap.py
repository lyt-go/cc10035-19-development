"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.incident_dispatch import _same_moment, incident_dispatch_service
from app.store import store


class DataGapService(BaseModuleService):
    MODULE = "data_gap"
    ENTITY = "断点记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "设备编号", "测点编号", "中断起始", "中断结束", "缺失点数", "补采策略", "处理状态"]
    REQUIRED_FIELDS = ["记录编号", "设备编号", "测点编号"]
    STATUS_ORDER = ["待补采", "补采中", "已补齐", "放弃补采"]
    ACTION_RULES = {"发起补采": "补采中", "标记补齐": "已补齐", "放弃补采": "放弃补采"}
    NEGATIVE_ACTIONS = []

    def list_entries(self, *, filters: dict[str, str] | None = None, **kwargs: Any):
        """保留通用字段契约，同时支持按实际网关/设备身份/时刻做复合过滤。"""
        safe_filters = dict(filters or {})
        gateway = (
            safe_filters.pop("网关编号", None)
            or safe_filters.pop("gateway_id", None)
            or safe_filters.pop("gatewayId", None)
        )
        identity = (
            safe_filters.pop("设备身份", None)
            or safe_filters.pop("device_identity", None)
            or safe_filters.pop("device_id", None)
            or safe_filters.pop("deviceId", None)
        )
        moment = (
            safe_filters.pop("缺失时刻", None)
            or safe_filters.pop("sample_time", None)
            or safe_filters.pop("sampleTime", None)
            or safe_filters.pop("时刻", None)
        )
        page = int(kwargs.get("page", 1))
        size = int(kwargs.get("size", 20))
        rows, _ = super().list_entries(
            filters=safe_filters,
            keyword=kwargs.get("keyword"),
            status=kwargs.get("status"),
            page=1,
            size=1_000_000,
        )
        if gateway:
            rows = [row for row in rows if incident_dispatch_service.gateway_of(row) == str(gateway)]
        if identity:
            expected = str(identity)
            rows = [
                row for row in rows
                if incident_dispatch_service.device_identity_of(row) == expected
                or incident_dispatch_service._device_matches(row, expected)
            ]
        if moment:
            rows = [row for row in rows if _same_moment(incident_dispatch_service.gap_time_of(row), moment)]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def run_action(self, entry_id: int, action: str, payload: dict[str, Any] | None = None):
        """数据断点动作：补采/补齐都经过统一幂等内核，避免产生两份确认读数。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "发起补采":
            result = incident_dispatch_service.initiate_single_recovery(entry)
            return entry, result["message"]

        if action == "标记补齐":
            receipt_payload = dict(payload or {})
            result = incident_dispatch_service.receive_receipt(entry, receipt_payload)
            if result.get("ok"):
                return entry, result["message"]
            # 无法形成签收证据时保留旧的状态流转兜底。
            return super().run_action(entry_id, action)

        return super().run_action(entry_id, action)

    def initiate_recovery(self, gap_identifier: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["source_type"] = "single_application"
        return incident_dispatch_service.initiate_recovery(gap_identifier, *args, **kwargs)

    def initiate_single_recovery(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["source_type"] = "single_application"
        return incident_dispatch_service.initiate_single_recovery(*args, **kwargs)

    trigger_recovery = initiate_recovery
    trigger_gap_recovery = initiate_recovery
    start_recovery = initiate_recovery

    def receive_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.receive_receipt(*args, **kwargs)

    confirm_recovery = receive_receipt
    sign_recovery = receive_receipt
    late_receipt = receive_receipt

    def recovery_status(self, gap_identifier: Any, **kwargs: Any) -> dict[str, Any] | None:
        gap = incident_dispatch_service._resolve_gap(gap_identifier, **kwargs)
        if gap is None:
            return None
        return incident_dispatch_service.gap_view(gap)

    # 数据断点是补采执行内核；派工编排仍委托给统一的事故派工服务。
    def dispatch_from_incident(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.dispatch_from_incident(*args, **kwargs)

    def claim_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.claim_dispatch(*args, **kwargs)

    def get_dispatch_detail(self, order_id: Any) -> dict[str, Any] | None:
        return incident_dispatch_service.get_dispatch_detail(order_id)

    def workbench(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.workbench(*args, **kwargs)
