"""事故补采派工演示场景。

固定构造 G1/G2 两个实际网关和同名设备 D：
- G1 下设备身份为 DEV-G1-D，14:02、14:05 两个时刻缺口；
- G2 下同名设备身份为 DEV-G2-D，也放一个同时刻缺口，用于验证邻站隔离。

这里直接写入内存表，保证服务重启后的演示/测试初始状态稳定。
"""
from __future__ import annotations

from typing import Any

SCENARIO_DATE = "2026-11-26"
SCENARIO_INCIDENT_ID = "INC-G1-1400"


def install_incident_scenario(tables: dict[str, list[dict[str, Any]]]) -> None:
    """幂等安装事故派工场景；已存在相同业务编号时不重复插入。"""

    gateways = tables.setdefault("gateway_mgmt", [])
    _upsert_by_business_id(
        gateways,
        "网关编号",
        "G1",
        {
            "id": _next_id(gateways),
            "status": "处理中",
            "pending": True,
            "abnormal": True,
            "网关编号": "G1",
            "网关地址": "一号站边缘网关",
            "挂载设备数": "1",
            "上行带宽": "100Mbps",
            "下行带宽": "100Mbps",
            "健康状态": "故障",
            "最后心跳": f"{SCENARIO_DATE}T13:59:30+08:00",
            "部署位置": "一号站",
            "实际网关编号": "G1",
        },
    )
    _upsert_by_business_id(
        gateways,
        "网关编号",
        "G2",
        {
            "id": _next_id(gateways),
            "status": "在线",
            "pending": False,
            "abnormal": False,
            "网关编号": "G2",
            "网关地址": "邻站边缘网关",
            "挂载设备数": "1",
            "上行带宽": "100Mbps",
            "下行带宽": "100Mbps",
            "健康状态": "健康",
            "最后心跳": f"{SCENARIO_DATE}T14:09:00+08:00",
            "部署位置": "邻站",
            "实际网关编号": "G2",
        },
    )

    devices = tables.setdefault("device_registry", [])
    _upsert_by_business_id(
        devices,
        "设备身份",
        "DEV-G1-D",
        {
            "id": _next_id(devices),
            "status": "离线",
            "pending": True,
            "abnormal": True,
            "设备编号": "D",
            "设备名称": "D",
            "设备身份": "DEV-G1-D",
            "设备类型": "工况采集终端",
            "协议类型": "MQTT",
            "认证方式": "证书",
            "所属网关": "G1",
            "实际网关编号": "G1",
            "站点": "一号站",
            "固件版本": "v2.4.1",
            "注册时间": f"{SCENARIO_DATE}T08:00:00+08:00",
            "最后在线": f"{SCENARIO_DATE}T14:00:05+08:00",
        },
    )
    _upsert_by_business_id(
        devices,
        "设备身份",
        "DEV-G2-D",
        {
            "id": _next_id(devices),
            "status": "在线",
            "pending": False,
            "abnormal": False,
            "设备编号": "D",
            "设备名称": "D",
            "设备身份": "DEV-G2-D",
            "设备类型": "工况采集终端",
            "协议类型": "MQTT",
            "认证方式": "证书",
            "所属网关": "G2",
            "实际网关编号": "G2",
            "站点": "邻站",
            "固件版本": "v2.4.1",
            "注册时间": f"{SCENARIO_DATE}T08:00:00+08:00",
            "最后在线": f"{SCENARIO_DATE}T14:09:50+08:00",
        },
    )

    gaps = tables.setdefault("data_gap", [])
    gap_specs = [
        ("GAP-G1-D-1402", "G1", "DEV-G1-D", "14:02", "待补采"),
        ("GAP-G1-D-1405", "G1", "DEV-G1-D", "14:05", "待补采"),
        ("GAP-G2-D-1402", "G2", "DEV-G2-D", "14:02", "待补采"),
    ]
    for record_id, gateway_id, device_identity, clock, status in gap_specs:
        hour, minute = clock.split(":")
        start = f"{SCENARIO_DATE}T{clock}:00+08:00"
        end_minute = int(minute) + 1
        end = f"{SCENARIO_DATE}T{hour}:{end_minute:02d}:00+08:00"
        _upsert_by_business_id(
            gaps,
            "记录编号",
            record_id,
            {
                "id": _next_id(gaps),
                "status": status,
                "pending": status != "已补齐",
                "abnormal": True,
                "记录编号": record_id,
                "断点编号": record_id,
                "网关编号": gateway_id,
                "实际网关编号": gateway_id,
                "所属网关": gateway_id,
                "设备编号": "D",
                "设备名称": "D",
                "设备身份": device_identity,
                "测点编号": "P-D",
                "缺失时刻": start,
                "样本时刻": start,
                "时刻": clock,
                "采样时刻": start,
                "中断起始": start,
                "中断结束": end,
                "缺失点数": 1,
                "补采策略": "远程补采并校验签收",
                "处理状态": status,
                "幂等键": f"{gateway_id}|{device_identity}|P-D|{clock}",
            },
        )

    incidents = tables.setdefault("gateway_incident", [])
    incident_entry = {
        "id": 1,
        "status": "处理中",
        "pending": True,
        "abnormal": True,
        "事故编号": SCENARIO_INCIDENT_ID,
        "事故号": SCENARIO_INCIDENT_ID,
        "网关事故号": SCENARIO_INCIDENT_ID,
        "事故名称": "G1 14:00-14:10 采集中断事故",
        "网关编号": "G1",
        "实际网关编号": "G1",
        "故障开始": "14:00",
        "故障结束": "14:10",
        "开始时间": f"{SCENARIO_DATE}T14:00:00+08:00",
        "结束时间": f"{SCENARIO_DATE}T14:10:00+08:00",
        "圈定设备": ["D"],
        "圈定设备编号": ["D"],
        "圈定设备身份": ["DEV-G1-D"],
        "影响测点": ["P-D"],
        "事故明细": "G1 在故障边界内缺失设备 D 的 14:02、14:05 确认读数。",
    }
    _upsert_by_business_id(incidents, "事故编号", SCENARIO_INCIDENT_ID, incident_entry)
    # 近义表名共享同一个 list，避免不同调用约定看到两份数据。
    for alias in (
        "gateway_incidents", "gateway_accident", "gateway_accidents",
        "incident", "incidents",
    ):
        tables[alias] = incidents

    # 这些表由事故派工服务按需写入；这里仅确保表存在并同步常见近义表名。
    dispatch_orders = tables.setdefault("incident_dispatch", [])
    for alias in (
        "recovery_dispatch", "gap_dispatch", "dispatch_order",
        "recovery_dispatch_order", "gap_dispatch_order", "补采派工单",
    ):
        tables[alias] = dispatch_orders

    dispatch_items = tables.setdefault("incident_dispatch_item", [])
    for alias in ("dispatch_item", "recovery_dispatch_item", "gap_dispatch_item", "派工明细"):
        tables[alias] = dispatch_items

    recovery_requests = tables.setdefault("recovery_request", [])
    for alias in ("recovery_order", "gap_recovery", "single_recovery", "补采申请单"):
        tables[alias] = recovery_requests

    evidences = tables.setdefault("recovery_evidence", [])
    for alias in ("completion_evidence", "完成证据"):
        tables[alias] = evidences

    readings = tables.setdefault("confirmed_reading", [])
    for alias in ("confirmed_readings", "unique_reading", "确认读数"):
        tables[alias] = readings


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1


def _upsert_by_business_id(
    rows: list[dict[str, Any]], field: str, value: str, entry: dict[str, Any]
) -> None:
    for row in rows:
        if str(row.get(field, "")) == value:
            # 保留运行期状态：测试/演示已经推进过的种子行不被重新安装覆盖。
            row.setdefault("_scenario_installed", True)
            return
    entry["_scenario_installed"] = True
    rows.append(entry)
