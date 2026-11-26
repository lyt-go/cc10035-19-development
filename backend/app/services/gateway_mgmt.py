"""网关管理领域服务：IoT网关节点的接入、监控与健康管理。

提供网关心跳、健康检查、负载统计、设备分配等真实业务能力。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.incident_dispatch import incident_dispatch_service
from app.store import store

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now(CST)


class GatewayMgmtService(BaseModuleService):
    MODULE = "gateway_mgmt"
    ENTITY = "网关节点"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["网关编号", "网关地址", "挂载设备数", "上行带宽", "下行带宽", "健康状态", "最后心跳", "部署位置"]
    REQUIRED_FIELDS = ["网关编号", "网关地址", "挂载设备数"]
    STATUS_ORDER = ["在线", "降级", "离线", "已退役"]
    ACTION_RULES = {"标记降级": "降级", "标记离线": "离线", "退役网关": "已退役"}
    NEGATIVE_ACTIONS = []

    def heartbeat(self, gateway_id: str) -> dict[str, Any]:
        """网关心跳上报：更新最后心跳时间和健康状态。"""
        rows = store.rows(self.MODULE)
        gw = None
        for row in rows:
            if row.get("网关编号") == gateway_id:
                gw = row
                break
        if gw is None:
            return {"ok": False, "message": f"网关 {gateway_id} 不存在"}

        gw["最后心跳"] = _now()
        gw["健康状态"] = "健康"
        if gw["status"] == "离线":
            gw["status"] = "在线"
        gw["pending"] = True
        gw["abnormal"] = False

        return {
            "ok": True,
            "message": f"网关 {gateway_id} 心跳已更新",
            "网关编号": gateway_id,
            "最后心跳": gw["最后心跳"],
            "健康状态": gw["健康状态"],
            "状态": gw["status"],
        }

    def health_check(self, gateway_id: str) -> dict[str, Any]:
        """网关健康检查：检测带宽利用率、心跳超时和设备挂载数。

        返回健康分数和建议动作。
        """
        rows = store.rows(self.MODULE)
        gw = None
        for row in rows:
            if row.get("网关编号") == gateway_id:
                gw = row
                break
        if gw is None:
            return {"ok": False, "message": f"网关 {gateway_id} 不存在"}

        issues = []
        score = 100

        # 检查心跳超时
        last_heartbeat = gw.get("最后心跳", "")
        if last_heartbeat:
            try:
                last_dt = datetime.fromisoformat(last_heartbeat)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=CST)
                elapsed = (_now_dt() - last_dt).total_seconds()
                if elapsed > 300:
                    issues.append(f"心跳超时({int(elapsed)}秒)")
                    score -= 40
                    gw["健康状态"] = "异常"
                elif elapsed > 120:
                    issues.append(f"心跳延迟({int(elapsed)}秒)")
                    score -= 20
            except (ValueError, TypeError):
                pass

        # 检查带宽
        try:
            up_bw = float(gw.get("上行带宽", "0"))
            down_bw = float(gw.get("下行带宽", "0"))
            if up_bw < 1:
                issues.append("上行带宽不足")
                score -= 15
            if down_bw < 1:
                issues.append("下行带宽不足")
                score -= 15
        except (ValueError, TypeError):
            pass

        # 检查设备挂载数
        try:
            device_count = int(gw.get("挂载设备数", "0"))
            if device_count > 1000:
                issues.append(f"挂载设备数过多({device_count})")
                score -= 10
        except (ValueError, TypeError):
            pass

        # 确定健康等级
        if score >= 80:
            level = "健康"
        elif score >= 60:
            level = "需关注"
            gw["健康状态"] = "需关注"
        else:
            level = "异常"
            gw["健康状态"] = "异常"

        return {
            "ok": True,
            "gateway_id": gateway_id,
            "health_score": max(score, 0),
            "health_level": level,
            "issues": issues,
            "建议动作": "正常" if score >= 80 else "需检查",
        }

    def load_statistics(self) -> dict[str, Any]:
        """网关负载统计：在线网关负载分布。"""
        rows = store.rows(self.MODULE)
        online = [row for row in rows if row.get("status") == "在线"]

        total_devices = 0
        total_up = 0.0
        total_down = 0.0
        for gw in online:
            try:
                total_devices += int(gw.get("挂载设备数", "0"))
            except (ValueError, TypeError):
                pass
            try:
                total_up += float(gw.get("上行带宽", "0"))
            except (ValueError, TypeError):
                pass
            try:
                total_down += float(gw.get("下行带宽", "0"))
            except (ValueError, TypeError):
                pass

        return {
            "total_gateways": len(rows),
            "online_gateways": len(online),
            "total_mounted_devices": total_devices,
            "total_upstream_bandwidth_mbps": round(total_up, 2),
            "total_downstream_bandwidth_mbps": round(total_down, 2),
            "avg_devices_per_gateway": round(total_devices / max(len(online), 1), 1),
            "gateways": [
                {
                    "网关编号": gw.get("网关编号"),
                    "网关地址": gw.get("网关地址"),
                    "挂载设备数": gw.get("挂载设备数"),
                    "健康状态": gw.get("健康状态"),
                    "最后心跳": gw.get("最后心跳"),
                }
                for gw in online
            ],
        }

    def assign_device(self, gateway_id: str, device_count: int = 1) -> dict[str, Any]:
        """为网关分配设备：增加挂载设备数并检查负载。"""
        rows = store.rows(self.MODULE)
        gw = None
        for row in rows:
            if row.get("网关编号") == gateway_id:
                gw = row
                break
        if gw is None:
            return {"ok": False, "message": f"网关 {gateway_id} 不存在"}

        try:
            current = int(gw.get("挂载设备数", "0"))
        except (ValueError, TypeError):
            current = 0

        new_count = current + device_count
        if new_count > 1000:
            return {
                "ok": False,
                "message": f"网关 {gateway_id} 设备数将达到 {new_count}，超过限制1000",
            }

        gw["挂载设备数"] = str(new_count)
        return {
            "ok": True,
            "message": f"网关 {gateway_id} 已分配 {device_count} 个设备",
            "网关编号": gateway_id,
            "变更前": current,
            "变更后": new_count,
        }

    # ========== 跨模块方法 ==========

    def manage_cache_for_gateway(self, gateway_id: str, cache_size: str, eviction_strategy: str) -> dict[str, Any]:
        """在网关上配置边缘缓存，根据网关健康状态调整缓存策略。

        业务流程:
        1. 查找网关节点
        2. 检查健康状态
        3. 创建/更新边缘缓存配置
        """
        gateway_rows = store.rows(self.MODULE)
        cache_rows = store.rows("edge_cache")

        gateway = None
        for row in gateway_rows:
            if row.get("网关编号") == gateway_id:
                gateway = row
                break
        if gateway is None:
            return {"ok": False, "message": f"网关 {gateway_id} 不存在"}

        gw_status = gateway.get("status", "")
        health = gateway.get("健康状态", "")

        # 降级时缩容
        adjusted_size = cache_size
        if gw_status == "降级" or health == "异常":
            try:
                adjusted_size = str(int(cache_size.replace("MB", "")) // 2) + "MB"
            except (ValueError, TypeError):
                adjusted_size = "128MB"
            eviction_strategy = "LRU" if eviction_strategy == "LFU" else eviction_strategy

        # 更新缓存配置
        existing = None
        for row in cache_rows:
            if row.get("网关编号") == gateway_id:
                existing = row
                break

        if existing:
            existing["缓存大小"] = adjusted_size
            existing["淘汰策略"] = eviction_strategy
            existing["最近刷新"] = _now()
            cache_entry = existing
        else:
            max_id = max((int(row.get("id", 0)) for row in cache_rows), default=0)
            cache_entry = {
                "id": max_id + 1,
                "配置编号": f"ECA-{gateway_id}",
                "网关编号": gateway_id,
                "缓存大小": adjusted_size,
                "淘汰策略": eviction_strategy,
                "持久化开关": "on",
                "同步间隔": "60s",
                "压缩开关": "on",
                "最近刷新": _now(),
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            cache_rows.append(cache_entry)

        gateway["健康状态"] = "健康"

        return {
            "ok": True,
            "message": f"网关 {gateway_id} 边缘缓存已配置（{adjusted_size}, {eviction_strategy}）",
            "gateway": {"网关编号": gateway_id, "状态": gw_status},
            "cache_entry": {"配置编号": cache_entry.get("配置编号"), "缓存大小": adjusted_size, "淘汰策略": eviction_strategy},
        }

    def device_distribution_by_gateway(self) -> list[dict[str, Any]]:
        """统计各网关下挂的设备分布情况。

        业务: 关联device_registry中设备的所属网关字段。
        """
        gateway_rows = store.rows(self.MODULE)
        device_rows = store.rows("device_registry")
        result = []
        for gw in gateway_rows:
            gw_id = gw.get("网关编号")
            devices = [
                d.get("设备编号") for d in device_rows
                if d.get("所属网关") == gw_id or d.get("所属网关") == gw.get("网关地址")
            ]
            result.append({
                "网关编号": gw_id,
                "网关地址": gw.get("网关地址"),
                "网关状态": gw.get("status"),
                "已挂载设备数": gw.get("挂载设备数"),
                "已注册关联设备数": len(devices),
            })
        return result

    # ========== 网关事故 → 补采派工 ==========
    # 网关管理页只做入口和查询，完整不变量在 IncidentDispatchService 中实现。

    def list_gateway_incidents(self) -> list[dict[str, Any]]:
        return incident_dispatch_service.list_incidents()

    def get_incident_detail(self, incident_id: Any = None, **filters: Any) -> dict[str, Any]:
        incident = incident_dispatch_service.get_incident(incident_id, **filters)
        if incident is None:
            return {"ok": False, "message": "网关事故不存在"}
        incident_code = incident.get("事故编号")
        return {
            "ok": True,
            "incident": incident,
            "items": incident_dispatch_service.query_gaps(incident),
            "dispatchable_items": incident_dispatch_service.query_dispatchable_gaps(incident),
            "workbench": incident_dispatch_service.workbench(incident_code),
        }

    def query_incident_dispatchable_gaps(self, *args: Any, **kwargs: Any):
        return incident_dispatch_service.query_dispatchable_gaps(*args, **kwargs)

    get_dispatchable_gaps_for_incident = query_incident_dispatchable_gaps
    list_dispatch_positions = query_incident_dispatchable_gaps

    def create_dispatch_order_from_incident(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.create_dispatch_order(*args, **kwargs)

    create_dispatch_order = create_dispatch_order_from_incident
    create_gap_dispatch_order = create_dispatch_order_from_incident
    dispatch_gap_recovery_from_incident = create_dispatch_order_from_incident

    def claim_recovery_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.claim_dispatch(*args, **kwargs)

    claim_gap_dispatch_order = claim_recovery_dispatch
    claim_dispatch_order = claim_recovery_dispatch

    def get_recovery_dispatch_detail(self, order_id: Any) -> dict[str, Any] | None:
        return incident_dispatch_service.get_dispatch_detail(order_id)

    get_dispatch_detail = get_recovery_dispatch_detail

    def refresh_recovery_dispatch(self, order_id: Any) -> dict[str, Any] | None:
        return incident_dispatch_service.refresh_dispatch_detail(order_id)

    refresh_dispatch_detail = refresh_recovery_dispatch

    def incident_dispatch_workbench(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.workbench(*args, **kwargs)

    get_dispatch_workbench = incident_dispatch_workbench

    def receive_recovery_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return incident_dispatch_service.receive_receipt(*args, **kwargs)

    receive_dispatch_receipt = receive_recovery_receipt
