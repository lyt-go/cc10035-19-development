"""事故补采派工专用接口。

通用资源接口只能按模块登记/流转；这里暴露事故明细、圈定缺口、迟到签收
和共享证据等聚合动作。路由同时保留几组近义路径，便于从网关事故或补采派工入口接入。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query

from app.services.incident_dispatch import incident_dispatch_service as svc

router = APIRouter(tags=["事故补采派工"])


def _payload_value(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容测试/前端可能使用的 camelCase 与中文键。"""
    normalized = dict(payload)
    mapping = {
        "incidentNo": "incident_id",
        "accidentNo": "incident_id",
        "gatewayIncidentNo": "incident_id",
        "gatewayId": "gateway_id",
        "gatewayNo": "gateway_id",
        "gwId": "gateway_id",
        "deviceId": "device_id",
        "deviceNo": "device_id",
        "deviceIdentity": "device_identity",
        "pointId": "point_id",
        "sampleTime": "sample_time",
        "missingTime": "sample_time",
        "startTime": "start_time",
        "endTime": "end_time",
        "gapIds": "gap_ids",
        "selectedGapIds": "gap_ids",
        "requestId": "request_id",
        "originalRequestId": "original_request_id",
        "receiptId": "receipt_id",
        "orderId": "order_id",
        "itemId": "item_id",
    }
    for source, target in mapping.items():
        if source in payload and target not in normalized:
            normalized[target] = payload[source]
    return normalized


# ---------- 静态查询路径必须放在 {incident_id} 动态路径之前 ----------

@router.get("/api/incident-dispatch/incidents")
@router.get("/api/gateway-incidents")
async def list_incidents() -> dict[str, Any]:
    items = svc.list_incidents()
    return {"ok": True, "items": items, "data": items}


@router.get("/api/incident-dispatch/candidates")
@router.get("/api/gateway-incidents/candidates")
async def candidates(
    incident_id: str | None = None,
    gateway_id: str | None = None,
    device_id: str | None = None,
    sample_time: str | None = None,
) -> dict[str, Any]:
    items = svc.query_dispatchable_gaps(
        incident_id, gateway_id=gateway_id, device_id=device_id, sample_time=sample_time
    )
    return {"ok": True, "items": items, "data": items, "total": len(items)}


@router.get("/api/incident-dispatch/workbench")
@router.get("/api/recovery-dispatch/workbench")
@router.get("/api/gateway-incidents/workbench")
async def workbench(incident_id: str = Query(default="INC-G1-1400")) -> dict[str, Any]:
    return svc.workbench(incident_id)


@router.get("/api/incident-dispatch/incidents/{incident_id}")
@router.get("/api/gateway-incidents/{incident_id}")
async def incident_detail(incident_id: str) -> dict[str, Any]:
    incident = svc.get_incident(incident_id)
    if incident is None:
        return {"ok": False, "message": "网关事故不存在"}
    return {
        "ok": True,
        "incident": incident,
        "detail": svc.workbench(incident_id),
        "items": svc.query_gaps(incident),
        "dispatchable_items": svc.query_dispatchable_gaps(incident),
    }


@router.post("/api/incident-dispatch/dispatch")
@router.post("/api/incident-dispatch/claim")
@router.post("/api/recovery-dispatch/orders")
@router.post("/api/gateway-incidents/dispatch")
async def dispatch(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    payload = _normalize_payload(payload)
    incident_id = _payload_value(payload, "incident_id", "事故编号", "事故号")
    gap_ids = _payload_value(payload, "gap_ids", "selected_gap_ids", "selected", "断点编号", "gap_id")
    return svc.claim_dispatch(
        incident_id,
        gap_ids,
        **payload,
    )


@router.get("/api/incident-dispatch/orders/{order_id}")
@router.get("/api/recovery-dispatch/orders/{order_id}")
async def order_detail(order_id: str) -> dict[str, Any]:
    detail = svc.refresh_dispatch_detail(order_id)
    if detail is None:
        return {"ok": False, "message": "派工单不存在"}
    return detail


@router.post("/api/incident-dispatch/orders/{order_id}/execute")
@router.post("/api/recovery-dispatch/orders/{order_id}/execute")
async def execute_order_item(
    order_id: str, payload: dict[str, Any] = Body(default_factory=dict)
) -> dict[str, Any]:
    return svc.execute_item(
        order_id,
        payload.get("item_id") or payload.get("gap_id") or payload.get("时刻"),
        **payload,
    )


@router.post("/api/incident-dispatch/orders/{order_id}/verify")
async def verify_order_source(
    order_id: str, payload: dict[str, Any] = Body(default_factory=dict)
) -> dict[str, Any]:
    return svc.verify_source(
        order_id, payload.get("item_id") or payload.get("gap_id"), **payload
    )


@router.post("/api/incident-dispatch/orders/{order_id}/close")
async def close_order(order_id: str) -> dict[str, Any]:
    return svc.close_dispatch_order(order_id)


@router.post("/api/incident-dispatch/single-recovery")
@router.post("/api/data-gap/recovery")
async def single_recovery(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    payload = _normalize_payload(payload)
    gap_id = _payload_value(payload, "gap_id", "记录编号", "断点编号")
    return svc.initiate_single_recovery(gap_id, **payload)


@router.post("/api/incident-dispatch/receipts")
@router.post("/api/recovery-dispatch/receipts")
@router.post("/api/data-gap/receipt")
async def receive_receipt(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    payload = _normalize_payload(payload)
    receipt_id = _payload_value(
        payload, "request_id", "申请编号", "recovery_request_id", "original_request_id"
    )
    return svc.receive_receipt(receipt_id, payload)
