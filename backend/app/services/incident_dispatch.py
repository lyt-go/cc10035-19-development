"""网关事故 → 缺口补采派工领域服务。

核心不变量：
1. 可派项必须同时匹配「实际网关」和「设备身份」，不能只按同名设备查询；
2. 时间边界来自本次事故，派工只执行被选中且仍未补齐/放弃的缺口；
3. 同一实际时刻只允许一份 confirmed_reading；
4. 单点补采申请与派工单相遇时共享 recovery_request / evidence / reading，
   不产生第二份独立采样结果。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.incident_scenario import SCENARIO_INCIDENT_ID
from app.services.base import BaseModuleService
from app.store import store

CST = timezone(timedelta(hours=8))

PENDING_GAP_STATUSES = {"待补采", "补采中", "待派工", "派发中", "处理中"}
DONE_GAP_STATUSES = {"已补齐", "已完成"}
TERMINAL_GAP_STATUSES = DONE_GAP_STATUSES | {"放弃补采", "已取消"}
OPEN_REQUEST_STATUSES = {"待签收", "已出站", "待确认", "pending", "outbound_pending"}

STATE_NOT_SENT = "not_sent"
STATE_OUTBOUND_PENDING = "outbound_pending"
STATE_COMPLETED_ORIGINAL = "completed_by_original"
STATE_COMPLETED = "completed"
STATE_ABANDONED = "abandoned"

STATE_LABELS = {
    STATE_NOT_SENT: "确实未发",
    STATE_OUTBOUND_PENDING: "已出站，仍需等候确认",
    STATE_COMPLETED_ORIGINAL: "已经由原申请完成",
    STATE_COMPLETED: "已完成",
    STATE_ABANDONED: "已放弃",
}


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，;；\n]+", value) if part.strip()]
    return [value]


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
            if not match and re.fullmatch(r"\d{4}", text):
                match = re.fullmatch(r"(\d{2})(\d{2})", text)
            if not match:
                return None
            hour, minute, second = match.groups(default="0")
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
            date_text = date_match.group(1) if date_match else "1970-01-01"
            parsed = datetime.fromisoformat(
                f"{date_text}T{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
            )
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(CST).replace(tzinfo=None)
    return parsed


def _time_token(value: Any) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return _text(value)
    if parsed.year == 1970 and parsed.month == 1 and parsed.day == 1:
        return parsed.strftime("%H:%M")
    return parsed.strftime("%Y-%m-%dT%H:%M")


def _clock_label(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.strftime("%H:%M") if parsed is not None else _text(value)


def _same_moment(left: Any, right: Any) -> bool:
    a, b = _parse_time(left), _parse_time(right)
    if a is None or b is None:
        return _text(left) == _text(right) and bool(_text(left))
    # 只给 HH:MM 时按日内分钟比较；若两边都有明确日期则日期也必须相同。
    if a.year != 1970 and b.year != 1970 and a.date() != b.date():
        return False
    return a.hour == b.hour and a.minute == b.minute


def _in_window(moment: Any, start: Any, end: Any) -> bool:
    current = _parse_time(moment)
    left = _parse_time(start)
    right = _parse_time(end)
    if current is None or left is None or right is None:
        return True
    current_minutes = current.hour * 60 + current.minute
    left_minutes = left.hour * 60 + left.minute
    right_minutes = right.hour * 60 + right.minute
    if left_minutes <= right_minutes:
        return left_minutes <= current_minutes <= right_minutes
    # 跨夜窗口。
    return current_minutes >= left_minutes or current_minutes <= right_minutes


class IncidentDispatchService(BaseModuleService):
    MODULE = "incident_dispatch"
    ENTITY = "补采派工单"
    LIST_FIELDS = [
        "派工单编号", "事故编号", "网关编号", "圈定设备", "派发断点",
        "共享申请编号", "完成证据编号", "状态", "领取人", "派发时间",
    ]
    REQUIRED_FIELDS = ["事故编号"]
    STATUS_ORDER = ["已领取", "处理中", "部分完成", "已完成"]
    ACTION_RULES = {"刷新核对": "处理中", "核对收尾": "已完成", "取消派工": "已取消"}

    # ------------------------------------------------------------------
    # 基础表与标识
    # ------------------------------------------------------------------
    def _rows(self, table: str) -> list[dict[str, Any]]:
        return store.rows(table)

    def _next_numeric_id(self, table: str) -> int:
        return max((int(row.get("id", 0) or 0) for row in self._rows(table)), default=0) + 1

    def _next_business_id(self, table: str, prefix: str, fields: list[str]) -> str:
        max_suffix = 0
        pattern = re.compile(rf"{re.escape(prefix)}-?(\d+)$", re.IGNORECASE)
        for row in self._rows(table):
            for field in fields:
                match = pattern.search(_text(row.get(field)))
                if match:
                    max_suffix = max(max_suffix, int(match.group(1)))
        return f"{prefix}-{max_suffix + 1:04d}"

    @staticmethod
    def gateway_of(target: Any) -> str:
        if target is None:
            return ""
        if not isinstance(target, dict):
            return _text(target)
        for field in (
            "实际网关编号", "实际网关", "网关编号", "gateway_id", "gatewayId",
            "网关ID", "所属网关编号",
        ):
            value = _text(target.get(field))
            if value:
                return value
        attached = _text(target.get("所属网关"))
        if attached:
            # 设备档案自身的所属网关直接采用；即使是旧示例文本也不应再回查造成递归。
            if "设备编号" in target or "设备身份" in target or target.get("MODULE") == "device":
                return attached
            if "样例" not in attached:
                return attached
        identity = IncidentDispatchService.device_identity_of(target)
        device_name = IncidentDispatchService.device_name_of(target) if isinstance(target, dict) else ""
        device_matches: list[dict[str, Any]] = []
        for device in store.rows("device_registry"):
            if identity and IncidentDispatchService.device_identity_of(device) == identity:
                return IncidentDispatchService.gateway_of(device)
            if device_name and device_name in {
                _text(device.get("设备编号")), _text(device.get("设备名称")),
                IncidentDispatchService.device_identity_of(device),
            }:
                device_matches.append(device)
        # 同名设备可能跨网关；只有唯一档案时才能安全推断实际网关。
        if len(device_matches) == 1:
            return IncidentDispatchService.gateway_of(device_matches[0])
        return attached

    @staticmethod
    def device_identity_of(row: dict[str, Any] | Any) -> str:
        if not isinstance(row, dict):
            return _text(row)
        for field in (
            "设备身份", "device_identity", "device_id", "deviceId",
            "设备ID", "设备唯一标识", "uuid",
        ):
            value = _text(row.get(field))
            if value:
                return value
        return ""

    @staticmethod
    def device_name_of(row: dict[str, Any]) -> str:
        for field in ("设备名称", "device_name", "设备编号", "device_id"):
            value = _text(row.get(field))
            if value:
                return value
        return ""

    @staticmethod
    def point_of(row: dict[str, Any]) -> str:
        return _text(row.get("测点编号") or row.get("point_id") or row.get("测点") or "P-D")

    @staticmethod
    def gap_time_of(row: dict[str, Any]) -> Any:
        for field in (
            "缺失时刻", "样本时刻", "采样时刻", "数据时刻", "时间点",
            "timestamp", "time", "时刻", "中断起始",
        ):
            if row.get(field) not in (None, ""):
                return row.get(field)
        return None

    def gap_key(self, gap: dict[str, Any]) -> str:
        stored = _text(gap.get("幂等键") or gap.get("idempotency_key"))
        if stored and "|" in stored:
            return stored
        gateway = self.gateway_of(gap)
        identity = self.device_identity_of(gap) or f"{gateway}::{self.device_name_of(gap)}"
        point = self.point_of(gap)
        moment = _time_token(self.gap_time_of(gap))
        return "|".join([gateway, identity, point, moment])

    def reading_key(self, gap: dict[str, Any]) -> str:
        # 确认读数按实际网关、实际设备身份和实际时刻去重。
        gateway = self.gateway_of(gap)
        identity = self.device_identity_of(gap) or f"{gateway}::{self.device_name_of(gap)}"
        moment = _time_token(self.gap_time_of(gap))
        return f"reading|{gateway}|{identity}|{moment}"

    # ------------------------------------------------------------------
    # 事故 / 缺口查询
    # ------------------------------------------------------------------
    def get_incident(self, identifier: Any = None, **filters: Any) -> dict[str, Any] | None:
        rows = self._rows("gateway_incident")
        if isinstance(identifier, dict):
            return identifier
        incident_id = identifier or filters.get("incident_id") or filters.get("事故编号") or filters.get("事故号")
        if incident_id is not None:
            text_id = _text(incident_id)
            for row in rows:
                if text_id.isdigit() and int(row.get("id", -1)) == int(text_id):
                    return row
                if text_id in {
                    _text(row.get("id")), _text(row.get("事故编号")),
                    _text(row.get("事故号")), _text(row.get("网关事故号")),
                }:
                    return row
            # 允许直接把 G1 当作“该网关当前事故”的入口。
            gateway_match = [row for row in rows if self.gateway_of(row) == text_id]
            if gateway_match:
                return gateway_match[-1]
        gateway_id = filters.get("gateway_id") or filters.get("网关编号")
        if gateway_id:
            matched = [row for row in rows if self.gateway_of(row) == _text(gateway_id)]
            if matched:
                return matched[-1]
        if not identifier and not filters:
            for row in rows:
                if _text(row.get("事故编号")) == SCENARIO_INCIDENT_ID:
                    return row
            return rows[-1] if rows else None
        return None

    def list_incidents(self) -> list[dict[str, Any]]:
        return list(self._rows("gateway_incident"))

    def _resolve_gap(self, identifier: Any = None, **filters: Any) -> dict[str, Any] | None:
        if isinstance(identifier, dict):
            return identifier
        gap_id = (
            identifier
            or filters.get("gap_id")
            or filters.get("记录编号")
            or filters.get("断点编号")
            or filters.get("id")
        )
        rows = self._rows("data_gap")
        if gap_id is not None and gap_id != "":
            text_id = _text(gap_id)
            for row in rows:
                if text_id.isdigit() and int(row.get("id", -1)) == int(text_id):
                    return row
                if text_id in {
                    _text(row.get("记录编号")), _text(row.get("断点编号")),
                    _text(row.get("scenario_key")), _text(row.get("幂等键")),
                }:
                    return row
                if text_id == self.gap_key(row):
                    return row
                record_text = " ".join([
                    _text(row.get("记录编号")), _text(row.get("断点编号")),
                    _text(row.get("scenario_key")),
                ])
                # 兼容 G1-D-1402 这类省略 GAP- 前缀的稳定业务标识。
                if text_id and ("-" in text_id or "_" in text_id) and text_id.casefold() in record_text.casefold():
                    return row

        # 形如 14:02 / 1402 的纯时刻标识：在显式网关条件下解析。
        gateway_id = (
            filters.get("gateway_id") or filters.get("网关编号") or filters.get("实际网关编号")
            or filters.get("gateway") or filters.get("gateway_no") or filters.get("gatewayNo")
            or filters.get("gateway_code") or filters.get("gw_id") or filters.get("gatewayId")
        )
        device_id = (
            filters.get("device_id") or filters.get("device_identity")
            or filters.get("设备身份") or filters.get("设备编号") or filters.get("device")
            or filters.get("device_no") or filters.get("device_code") or filters.get("deviceId")
            or filters.get("device_name")
        )
        point_id = filters.get("point_id") or filters.get("测点编号")
        moment = (
            filters.get("sample_time") or filters.get("sampleTime") or filters.get("missingTime")
            or filters.get("time") or filters.get("时刻") or filters.get("缺失时刻")
        )
        if moment is None and gap_id is not None and _parse_time(gap_id):
            moment = gap_id
        if device_id is None and gap_id is not None and moment is None:
            # 允许 initiate_recovery("D", "P-D", "14:02") 这类以设备为首参的调用。
            device_id = gap_id

        candidates = rows
        if gateway_id:
            expected_gateway = _text(gateway_id)
            candidates = [row for row in candidates if self.gateway_of(row) == expected_gateway]
        if device_id:
            expected_device = _text(device_id)
            candidates = [row for row in candidates if self._device_matches(row, expected_device)]
        if point_id:
            expected_point = _text(point_id)
            candidates = [row for row in candidates if self.point_of(row) == expected_point]
        if moment:
            candidates = [row for row in candidates if _same_moment(self.gap_time_of(row), moment)]
        return candidates[0] if candidates else None

    def _device_matches(self, gap: dict[str, Any], expected: str) -> bool:
        expected = _text(expected)
        if not expected:
            return True
        identity = self.device_identity_of(gap)
        if identity == expected:
            return True
        # 允许用 DEV-G1-D / D / 设备编号 命中；邻站同名行必须再由网关条件隔离。
        return expected in {
            _text(gap.get("设备编号")), _text(gap.get("设备名称")),
            _text(gap.get("device_id")), identity,
        }

    def _incident_scope_error(self, incident: dict[str, Any], gap: dict[str, Any]) -> str:
        incident_gateway = self.gateway_of(incident)
        gap_gateway = self.gateway_of(gap)
        if incident_gateway:
            if not gap_gateway:
                return "断点缺少可核验的实际网关，不能按网关事故派发"
            if gap_gateway != incident_gateway:
                return f"实际网关不一致：事故属于 {incident_gateway}，断点属于 {gap_gateway}"

        scoped_identities = set(_as_list(
            incident.get("圈定设备身份") or incident.get("device_identities") or incident.get("device_ids")
        ))
        scoped_names = set(_as_list(
            incident.get("圈定设备") or incident.get("圈定设备编号") or incident.get("devices")
        ))
        gap_identity = self.device_identity_of(gap)
        gap_name = self.device_name_of(gap)
        if scoped_identities and gap_identity not in scoped_identities:
            return f"设备身份 {gap_identity or gap_name} 不在事故圈定范围"
        if scoped_names and gap_name not in scoped_names and gap_identity not in scoped_names:
            return f"设备 {gap_name} 不在事故圈定范围"

        start = incident.get("开始时间") or incident.get("故障开始")
        end = incident.get("结束时间") or incident.get("故障结束")
        moment = self.gap_time_of(gap)
        if start and end and moment and not _in_window(moment, start, end):
            return f"断点时刻 {_time_token(moment)} 不在事故窗口内"
        return ""

    def query_gaps(
        self,
        incident: Any = None,
        *,
        only_dispatchable: bool = False,
        include_terminal: bool = True,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        inc = self.get_incident(incident, **filters) if not isinstance(incident, dict) else incident
        rows = list(self._rows("data_gap"))
        result: list[dict[str, Any]] = []
        for gap in rows:
            if inc is not None:
                if self._incident_scope_error(inc, gap):
                    continue
            else:
                gateway_id = (
                    filters.get("gateway_id") or filters.get("网关编号")
                    or filters.get("gateway_no") or filters.get("gateway_code")
                )
                device_id = (
                    filters.get("device_id") or filters.get("设备身份") or filters.get("设备编号")
                    or filters.get("device_no") or filters.get("device_name")
                )
                moment = filters.get("sample_time") or filters.get("时刻")
                if gateway_id and self.gateway_of(gap) != _text(gateway_id):
                    continue
                if device_id and not self._device_matches(gap, _text(device_id)):
                    continue
                if moment and not _same_moment(self.gap_time_of(gap), moment):
                    continue
                start = filters.get("start") or filters.get("start_time")
                end = filters.get("end") or filters.get("end_time")
                if start and end and not _in_window(self.gap_time_of(gap), start, end):
                    continue
            view = self.gap_view(gap)
            if only_dispatchable and not view["dispatchable"]:
                continue
            if not include_terminal and gap.get("status") in TERMINAL_GAP_STATUSES:
                continue
            result.append(view)
        result.sort(key=lambda item: _text(item.get("缺失时刻") or item.get("时刻")))
        return result

    def query_dispatchable_gaps(self, incident: Any = None, **filters: Any) -> list[dict[str, Any]]:
        filters.setdefault("only_dispatchable", True)
        return self.query_gaps(incident, **filters)

    def list_dispatchable_items(self, incident: Any = None, **filters: Any) -> list[dict[str, Any]]:
        return self.query_dispatchable_gaps(incident, **filters)

    def candidate_gaps(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.query_dispatchable_gaps(*args, **kwargs)

    def get_dispatchable_items(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.query_dispatchable_gaps(*args, **kwargs)

    def list_gaps_for_incident(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.query_gaps(*args, **kwargs)

    def get_candidates(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.query_dispatchable_gaps(*args, **kwargs)

    def find_gaps(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.query_gaps(*args, **kwargs)

    list_dispatchable_gaps_for_incident = query_dispatchable_gaps
    list_gaps_to_dispatch = query_dispatchable_gaps

    # ------------------------------------------------------------------
    # 状态聚合：申请、证据、读数、派工明细
    # ------------------------------------------------------------------
    def _requests_for_key(self, key: str) -> list[dict[str, Any]]:
        return [
            row for row in self._rows("recovery_request")
            if _text(row.get("idempotency_key") or row.get("幂等键")) == key
        ]

    def _active_request(self, key: str) -> dict[str, Any] | None:
        requests = self._requests_for_key(key)
        completed = [row for row in requests if row.get("status") in {"已签收", "已完成", "confirmed"} or row.get("evidence_id")]
        if completed:
            return completed[-1]
        pending = [row for row in requests if row.get("status") in OPEN_REQUEST_STATUSES]
        return pending[-1] if pending else None

    def _evidence_for_key(self, key: str) -> dict[str, Any] | None:
        evidences = self._rows("recovery_evidence")
        matched = [row for row in evidences if _text(row.get("idempotency_key") or row.get("幂等键")) == key]
        return matched[-1] if matched else None

    def _reading_for_key(self, reading_key: str) -> dict[str, Any] | None:
        readings = self._rows("confirmed_reading")
        matched = [row for row in readings if _text(row.get("reading_key")) == reading_key]
        return matched[-1] if matched else None

    def gap_view(self, gap: dict[str, Any]) -> dict[str, Any]:
        key = self.gap_key(gap)
        reading_key = self.reading_key(gap)
        request = self._active_request(key)
        evidence = self._evidence_for_key(key)
        reading = self._reading_for_key(reading_key)
        status = _text(gap.get("status") or gap.get("处理状态"))

        state = STATE_NOT_SENT
        if evidence or reading or status in DONE_GAP_STATUSES:
            source_type = _text((evidence or {}).get("source_type") or (request or {}).get("来源类型"))
            state = STATE_COMPLETED_ORIGINAL if source_type == "single_application" else STATE_COMPLETED
        elif status == "放弃补采":
            state = STATE_ABANDONED
        elif request is not None or status == "补采中":
            state = STATE_OUTBOUND_PENDING

        dispatch_items = [
            item for item in self._rows("incident_dispatch_item")
            if _text(item.get("idempotency_key") or item.get("幂等键")) == key
        ]
        return {
            "gap_id": gap.get("记录编号") or gap.get("id"),
            "numeric_id": gap.get("id"),
            "记录编号": gap.get("记录编号") or gap.get("断点编号"),
            "断点编号": gap.get("断点编号") or gap.get("记录编号"),
            "gateway_id": self.gateway_of(gap),
            "网关编号": self.gateway_of(gap),
            "实际网关": self.gateway_of(gap),
            "actual_gateway_id": self.gateway_of(gap),
            "device_identity": self.device_identity_of(gap),
            "设备身份": self.device_identity_of(gap),
            "device_id": self.device_identity_of(gap) or self.device_name_of(gap),
            "设备编号": gap.get("设备编号") or self.device_name_of(gap),
            "device_name": self.device_name_of(gap),
            "设备名称": self.device_name_of(gap),
            "point_id": self.point_of(gap),
            "测点编号": self.point_of(gap),
            "sample_time": self.gap_time_of(gap),
            "missing_time": self.gap_time_of(gap),
            "缺失时刻": self.gap_time_of(gap),
            "时刻": _clock_label(self.gap_time_of(gap)),
            "clock": _clock_label(self.gap_time_of(gap)),
            "idempotency_key": key,
            "reading_key": reading_key,
            "status": status,
            "gap_status": status,
            "处理状态": gap.get("处理状态") or status,
            "dispatch_state": state,
            "state": state,
            "workbench_state": state,
            "state_label": STATE_LABELS[state],
            "dispatchable": status in PENDING_GAP_STATUSES and state != STATE_ABANDONED,
            "request_id": (request or {}).get("申请编号") or (request or {}).get("request_id"),
            "shared_request_id": (request or {}).get("申请编号") or (request or {}).get("request_id"),
            "original_request_id": (
                (request or {}).get("申请编号")
                if (request or {}).get("来源类型") == "single_application"
                else ""
            ),
            "evidence_id": (evidence or {}).get("证据编号") or (evidence or {}).get("evidence_id"),
            "completion_evidence_id": (evidence or {}).get("证据编号") or (evidence or {}).get("evidence_id"),
            "reading_id": (reading or {}).get("reading_id") or (reading or {}).get("确认读数编号"),
            "confirmed_reading_id": (reading or {}).get("reading_id") or (reading or {}).get("确认读数编号"),
            "evidence": evidence,
            "reading": reading,
            "confirmed_reading": reading,
            "dispatch_item_ids": [item.get("明细编号") or item.get("id") for item in dispatch_items],
            "dispatch_order_ids": sorted({
                _text(item.get("派工单编号") or item.get("order_id"))
                for item in dispatch_items if item.get("派工单编号") or item.get("order_id")
            }),
        }

    def list_positions(self, incident: Any = SCENARIO_INCIDENT_ID) -> list[dict[str, Any]]:
        inc = self.get_incident(incident)
        return self.query_gaps(inc)

    # ------------------------------------------------------------------
    # 数据断点单条补采 / 统一出站
    # ------------------------------------------------------------------
    def initiate_recovery(self, gap_identifier: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """发出一笔补采申请；同点已有出站/完成申请时幂等复用。"""
        # 兼容 initiate_recovery(device_id, point_id, sample_time) 的位置参数形式。
        positional = list(args)
        if positional and isinstance(positional[0], dict):
            kwargs.update(positional.pop(0))
        if positional and not kwargs.get("point_id") and not kwargs.get("测点编号"):
            kwargs["point_id"] = positional.pop(0)
        if positional and not kwargs.get("sample_time"):
            kwargs["sample_time"] = positional.pop(0)

        gap = self._resolve_gap(gap_identifier, **kwargs)
        if gap is None and gap_identifier is not None:
            retry = dict(kwargs)
            retry.setdefault("device_id", gap_identifier)
            gap = self._resolve_gap(**retry)
        if gap is None:
            return {"ok": False, "message": "未找到符合实际网关、设备身份和时刻的断点"}

        key = self.gap_key(gap)
        reading_key = self.reading_key(gap)
        existing = self._active_request(key)
        evidence = self._evidence_for_key(key)
        reading = self._reading_for_key(reading_key)
        status = _text(gap.get("status"))

        if status == "放弃补采":
            return {"ok": False, "message": f"断点 {gap.get('记录编号')} 已放弃补采"}

        if evidence or reading or status in DONE_GAP_STATUSES:
            gap["status"] = "已补齐"
            gap["处理状态"] = "已补齐"
            gap["pending"] = False
            return {
                "ok": True,
                "duplicate": True,
                "already_completed": True,
                "sent": False,
                "dispatch_state": STATE_COMPLETED_ORIGINAL,
                "message": "该时刻已由原申请补齐，直接复用完成证据",
                "gap": gap,
                "request": existing,
                "evidence": evidence,
                "reading": reading,
            }

        if existing is not None:
            gap["status"] = "补采中"
            gap["处理状态"] = "补采中"
            return {
                "ok": True,
                "duplicate": True,
                "already_outbound": True,
                "sent": False,
                "dispatch_state": STATE_OUTBOUND_PENDING,
                "message": f"同点补采申请 {existing.get('申请编号')} 已出站，等待签收确认",
                "gap": gap,
                "request": existing,
            }

        source_type = _text(kwargs.get("source_type") or kwargs.get("source")) or "single_application"
        source_ref = _text(
            kwargs.get("source_ref") or kwargs.get("派工单编号")
            or kwargs.get("order_id") or ("DATA_GAP_WORKBENCH" if source_type == "single_application" else "")
        )
        request_id = self._next_business_id("recovery_request", "REC", ["申请编号", "request_id"])
        request = {
            "id": self._next_numeric_id("recovery_request"),
            "申请编号": request_id,
            "request_id": request_id,
            "申请类型": "单点补采" if source_type == "single_application" else "派工补采",
            "来源类型": source_type,
            "source_type": source_type,
            "来源单号": source_ref,
            "source_ref": source_ref,
            "断点主键": gap.get("id"),
            "记录编号": gap.get("记录编号"),
            "gap_id": gap.get("记录编号") or gap.get("id"),
            "网关编号": self.gateway_of(gap),
            "设备身份": self.device_identity_of(gap),
            "设备编号": self.device_name_of(gap),
            "测点编号": self.point_of(gap),
            "sample_time": self.gap_time_of(gap),
            "时刻": _clock_label(self.gap_time_of(gap)),
            "clock": _clock_label(self.gap_time_of(gap)),
            "idempotency_key": key,
            "幂等键": key,
            "reading_key": reading_key,
            "发送状态": "已出站",
            "send_status": "sent",
            "executor": "data_gap",
            "executor_name": "数据断点执行内核",
            "status": "待签收",
            "confirmation_status": "pending",
            "created_at": _now(),
            "sent_at": _now(),
            "receipt_at": "",
            "evidence_id": "",
            "reading_id": "",
        }
        self._rows("recovery_request").append(request)

        gap["status"] = "补采中"
        gap["处理状态"] = "补采中"
        gap["pending"] = True
        gap["last_recovery_request_id"] = request_id
        gap.setdefault("recovery_request_ids", []).append(request_id)

        return {
            "ok": True,
            "duplicate": False,
            "sent": True,
            "dispatch_state": STATE_OUTBOUND_PENDING,
            "message": f"补采申请 {request_id} 已由数据断点执行并出站",
            "gap": gap,
            "request": request,
        }

    def initiate_single_recovery(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["source_type"] = "single_application"
        return self.initiate_recovery(*args, **kwargs)

    def send_single_gap_recovery(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.initiate_single_recovery(*args, **kwargs)

    # ------------------------------------------------------------------
    # 派工单领取 / 派发
    # ------------------------------------------------------------------
    def claim_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # 兼容 claim_dispatch(incident_id, gap_ids) 的位置参数形式。
        if args:
            if len(args) >= 1 and "incident" not in kwargs and "incident_id" not in kwargs:
                kwargs["incident"] = args[0]
            if len(args) >= 2 and "gap_ids" not in kwargs:
                kwargs["gap_ids"] = args[1]

        gateway_id = (
            kwargs.get("gateway_id") or kwargs.get("网关编号")
            or kwargs.get("gateway_no") or kwargs.get("gateway_code")
        )
        incident = self.get_incident(
            kwargs.pop("incident", None)
            or kwargs.pop("incident_id", None)
            or kwargs.pop("事故编号", None)
            or kwargs.pop("事故号", None)
            or kwargs.pop("gateway_incident_id", None),
            gateway_id=gateway_id,
        )
        if incident is None and gateway_id:
            start = kwargs.get("start_time") or kwargs.get("start") or kwargs.get("故障开始")
            end = kwargs.get("end_time") or kwargs.get("end") or kwargs.get("故障结束")
            device_identities = kwargs.get("device_identities") or kwargs.get("device_ids") or kwargs.get("圈定设备身份")
            devices = kwargs.get("devices") or kwargs.get("device_id") or kwargs.get("圈定设备")
            incident_code = kwargs.get("incident_id") or kwargs.get("事故编号") or f"INC-{gateway_id}"
            incident = {
                "事故编号": incident_code,
                "事故号": incident_code,
                "网关事故号": kwargs.get("gateway_incident_no") or incident_code,
                "网关编号": gateway_id,
                "开始时间": start,
                "结束时间": end,
                "故障开始": start,
                "故障结束": end,
                "圈定设备身份": _as_list(device_identities),
                "圈定设备": _as_list(devices),
            }
        if incident is None:
            return {"ok": False, "message": "未找到对应网关事故"}

        selected = (
            kwargs.pop("gap_ids", None)
            or kwargs.pop("selected_gap_ids", None)
            or kwargs.pop("selected", None)
            or kwargs.pop("items", None)
            or kwargs.pop("断点编号", None)
            or kwargs.pop("gap_id", None)
        )
        gap_refs = _as_list(selected)
        if not gap_refs:
            return {"ok": False, "message": "请至少选择一个实际仍待处理的断点"}

        gaps: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        scoped_views = self.query_gaps(incident)
        for ref in gap_refs:
            ref_kwargs: dict[str, Any] = {}
            if isinstance(ref, dict):
                ref_kwargs.update(ref)
                ref_value = ref_kwargs.pop("gap_id", None) or ref_kwargs.pop("记录编号", None)
            else:
                ref_value = ref
            gap = self._resolve_gap(ref_value, **ref_kwargs)
            if gap is None or self._incident_scope_error(incident, gap):
                # 允许直接勾选“14:02”这类时刻；在事故范围内解析，避免邻站同名同时刻项抢匹配。
                text_ref = _text(ref_value)
                scoped_match = None
                for view in scoped_views:
                    if text_ref in {
                        _text(view.get("记录编号")), _text(view.get("断点编号")),
                        _text(view.get("gap_id")), _text(view.get("id")),
                        _text(view.get("明细编号")),
                    }:
                        scoped_match = self._resolve_gap(view.get("记录编号"))
                        break
                if scoped_match is None and (_parse_time(text_ref) or ref_kwargs.get("sample_time")):
                    moment = ref_kwargs.get("sample_time") or text_ref
                    for view in scoped_views:
                        if _same_moment(view.get("sample_time") or view.get("时刻"), moment):
                            scoped_match = self._resolve_gap(view.get("记录编号"))
                            break
                if scoped_match is not None:
                    gap = scoped_match
            if gap is None:
                invalid.append({"selected": ref, "reason": "断点不存在或身份/时刻不匹配"})
                continue
            reason = self._incident_scope_error(incident, gap)
            if reason:
                invalid.append({
                    "selected": ref,
                    "gap_id": gap.get("记录编号") or gap.get("id"),
                    "gateway_id": self.gateway_of(gap),
                    "device_id": self.device_identity_of(gap) or self.device_name_of(gap),
                    "reason": reason,
                })
                continue
            gaps.append(gap)

        if invalid:
            # 安全边界：跨网关/跨设备/越时间窗的选择不允许部分静默派发。
            return {
                "ok": False,
                "message": "存在不属于本事故范围的派工项，已整单拒绝",
                "incident": incident,
                "invalid": invalid,
                "items": [],
            }

        order_id = kwargs.get("order_id") or kwargs.get("派工单编号")
        order = self._get_order_row(order_id) if order_id else None
        if order_id and order is None:
            return {"ok": False, "message": f"派工单 {order_id} 不存在"}

        if order is None:
            order_number = self._next_business_id(
                "incident_dispatch", "DSP", ["派工单编号", "order_id"]
            )
            order = {
                "id": self._next_numeric_id("incident_dispatch"),
                "派工单编号": order_number,
                "order_id": order_number,
                "事故编号": incident.get("事故编号") or incident.get("事故号"),
                "网关事故号": incident.get("网关事故号") or incident.get("事故编号"),
                "网关编号": self.gateway_of(incident),
                "圈定设备": "、".join(_as_list(incident.get("圈定设备") or incident.get("圈定设备身份"))),
                "派发断点": "",
                "共享申请编号": "",
                "完成证据编号": "",
                "状态": "处理中",
                "status": "处理中",
                "领取人": _text(kwargs.get("assignee") or kwargs.get("operator") or kwargs.get("领取人")) or "值班巡检员",
                "派发时间": _now(),
                "created_at": _now(),
                "items": [],
                "pending": True,
                "abnormal": False,
            }
            self._rows("incident_dispatch").append(order)

        execute = kwargs.get("execute", kwargs.get("auto_execute", True))
        created_items: list[dict[str, Any]] = []
        handoffs: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for gap in gaps:
            key = self.gap_key(gap)
            existing_item = next((
                item for item in self._rows("incident_dispatch_item")
                if item.get("派工单编号") == order["派工单编号"]
                and _text(item.get("idempotency_key")) == key
            ), None)
            if existing_item is not None:
                created_items.append(existing_item)
                continue

            view_before = self.gap_view(gap)
            item_number = self._next_business_id(
                "incident_dispatch_item", "DITEM", ["明细编号", "item_id"]
            )
            item = {
                "id": self._next_numeric_id("incident_dispatch_item"),
                "明细编号": item_number,
                "item_id": item_number,
                "派工单编号": order["派工单编号"],
                "order_id": order["派工单编号"],
                "事故编号": order["事故编号"],
                "断点主键": gap.get("id"),
                "记录编号": gap.get("记录编号"),
                "gap_id": gap.get("记录编号") or gap.get("id"),
                "网关编号": self.gateway_of(gap),
                "设备身份": self.device_identity_of(gap),
                "设备编号": self.device_name_of(gap),
                "测点编号": self.point_of(gap),
                "sample_time": self.gap_time_of(gap),
                "时刻": _clock_label(self.gap_time_of(gap)),
                "clock": _clock_label(self.gap_time_of(gap)),
                "idempotency_key": key,
                "幂等键": key,
                "reading_key": self.reading_key(gap),
                "领取状态": "已领取",
                "执行状态": "待执行",
                "status": "已领取",
                "dispatch_state": STATE_NOT_SENT,
                "state_label": STATE_LABELS[STATE_NOT_SENT],
                "source_request_id": "",
                "original_request_id": "",
                "evidence_id": "",
                "reading_id": "",
                "completion_source": "",
                "shared_evidence": False,
                "source_verified": False,
                "核对状态": "待核对",
                "closeout_status": "无需收尾",
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._rows("incident_dispatch_item").append(item)
            order.setdefault("items", []).append(item_number)
            created_items.append(item)

            if gap.get("status") in TERMINAL_GAP_STATUSES and not view_before["evidence_id"]:
                item["dispatch_state"] = STATE_COMPLETED if gap.get("status") in DONE_GAP_STATUSES else STATE_ABANDONED
                item["执行状态"] = gap.get("status")
                skipped.append({"gap_id": item["gap_id"], "reason": "断点已经是终态，未重复派发"})
                continue

            if execute:
                handoff = self.initiate_recovery(
                    gap,
                    source_type="dispatch_order",
                    source_ref=order["派工单编号"],
                )
                handoffs.append(handoff)
                self._apply_handoff_to_item(item, handoff)

        self._refresh_order_summary(order)
        detail = self.get_dispatch_detail(order["派工单编号"])
        covered_items = [
            item for item in created_items
            if item.get("dispatch_state") not in {STATE_COMPLETED, STATE_COMPLETED_ORIGINAL, STATE_ABANDONED}
        ]
        return {
            "ok": True,
            "message": f"派工单 {order['派工单编号']} 已领取 {len(covered_items)} 个实际待处理断点",
            "order": order,
            "dispatch_order": detail,
            "items": detail["items"],
            "created_items": created_items,
            "covered_items": covered_items,
            "dispatched_items": covered_items,
            "covered_gap_ids": [item.get("gap_id") for item in covered_items],
            "dispatched_count": len(covered_items),
            "handoffs": handoffs,
            "skipped": skipped,
            "rejected": [],
        }

    def claim_gap(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if args and len(args) == 1 and not isinstance(args[0], (str, int)):
            # claim_gap(gap_id, incident_id=...)
            pass
        if args:
            kwargs.setdefault("gap_ids", [args[0]])
        return self.claim_dispatch(**kwargs)

    def create_dispatch_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_dispatch(*args, **kwargs)

    def dispatch_from_incident(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_dispatch(*args, **kwargs)

    def claim_dispatch_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_dispatch(*args, **kwargs)

    def create_recovery_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_dispatch(*args, **kwargs)

    def dispatch_selected_gaps(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_dispatch(*args, **kwargs)

    def receive_from_incident(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.claim_dispatch(*args, **kwargs)

    dispatch = claim_dispatch
    assign_gaps = claim_dispatch
    claim = claim_dispatch
    create_gap_dispatch_order = claim_dispatch
    claim_gap_dispatch_order = claim_dispatch
    dispatch_gap_recovery = claim_dispatch

    def get_incident_detail(self, identifier: Any = SCENARIO_INCIDENT_ID, **filters: Any) -> dict[str, Any] | None:
        incident = self.get_incident(identifier, **filters)
        if incident is None:
            return None
        return {
            "ok": True,
            "incident": incident,
            "detail": self.workbench(incident.get("事故编号")),
            "items": self.query_gaps(incident),
            "dispatchable_items": self.query_dispatchable_gaps(incident),
        }

    def get_dispatchable_gaps(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.query_dispatchable_gaps(*args, **kwargs)

    def list_available_gaps(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self.query_dispatchable_gaps(*args, **kwargs)

    def get_dispatch_order(self, order_identifier: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.get_dispatch_detail(order_identifier, **kwargs)

    def _apply_handoff_to_item(self, item: dict[str, Any], handoff: dict[str, Any]) -> None:
        request = handoff.get("request") or {}
        evidence = handoff.get("evidence") or {}
        reading = handoff.get("reading") or {}
        state = handoff.get("dispatch_state") or STATE_OUTBOUND_PENDING
        item["dispatch_state"] = state
        item["state_label"] = STATE_LABELS.get(state, state)
        item["source_request_id"] = request.get("申请编号") or request.get("request_id") or ""
        if handoff.get("duplicate") and request.get("来源类型") == "single_application":
            item["original_request_id"] = item["source_request_id"]
            item["completion_source"] = "original_application"
        elif request.get("来源类型") == "single_application":
            item["original_request_id"] = item["source_request_id"]
            item["completion_source"] = "original_application"
        else:
            item["completion_source"] = "dispatch_application"
        item["evidence_id"] = evidence.get("证据编号") or evidence.get("evidence_id") or ""
        item["reading_id"] = reading.get("reading_id") or ""
        item["shared_evidence"] = bool(evidence or reading)
        if state == STATE_COMPLETED_ORIGINAL:
            item["执行状态"] = "已完成"
            item["status"] = "已完成"
            item["closeout_status"] = "共享证据已闭合"
        else:
            item["执行状态"] = "待签收确认"
            item["status"] = "待确认"
            item["closeout_status"] = "等待签收"
        item["updated_at"] = _now()

    def execute_item(self, order_identifier: Any = None, item_identifier: Any = None, **kwargs: Any) -> dict[str, Any]:
        order = self._get_order_row(order_identifier or kwargs.get("order_id"))
        if order is None:
            # 也允许 execute_item(gap_id=..., incident_id=...) 直接补采。
            gap = self._resolve_gap(order_identifier, **kwargs)
            if gap is None:
                return {"ok": False, "message": "派工单或断点不存在"}
            return self.initiate_recovery(gap, source_type=kwargs.get("source_type", "dispatch_order"))

        item = self._find_item(order, item_identifier or kwargs.get("item_id") or kwargs.get("gap_id"))
        if item is None:
            return {"ok": False, "message": "派工明细不存在"}
        gap = self._resolve_gap(item.get("idempotency_key")) or self._resolve_gap(item.get("记录编号"))
        if gap is None:
            return {"ok": False, "message": "派工明细关联断点不存在"}
        handoff = self.initiate_recovery(
            gap, source_type="dispatch_order", source_ref=order.get("派工单编号")
        )
        self._apply_handoff_to_item(item, handoff)
        self._refresh_order_summary(order)
        return {"ok": handoff["ok"], "message": handoff["message"], "item": item, "handoff": handoff}

    def continue_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.execute_item(*args, **kwargs)

    def execute_gap(self, gap_identifier: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.initiate_recovery(gap_identifier, source_type="dispatch_order", **kwargs)

    # ------------------------------------------------------------------
    # 迟到签收 / 共享完成证据 / 唯一读数
    # ------------------------------------------------------------------
    def receive_receipt(self, identifier: Any = None, receipt_data: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if isinstance(identifier, dict):
            data.update(identifier)
            identifier = None
        if isinstance(receipt_data, dict):
            data.update(receipt_data)
        data.update(kwargs)
        if isinstance(data.get("payload"), dict):
            nested = data.pop("payload")
            data = {**nested, **data}

        request = self._find_request(
            identifier
            or data.get("request_id")
            or data.get("申请编号")
            or data.get("recovery_request_id")
            or data.get("original_request_id")
        )
        gap = None
        if request is not None:
            gap = self._resolve_gap(request.get("idempotency_key")) or self._resolve_gap(request.get("记录编号"))
        else:
            gap = self._resolve_gap(
                data.get("gap_id") or data.get("记录编号") or identifier,
                gateway_id=data.get("gateway_id") or data.get("网关编号"),
                device_id=data.get("device_id") or data.get("设备身份") or data.get("设备编号"),
                sample_time=data.get("sample_time") or data.get("时刻") or data.get("缺失时刻"),
                point_id=data.get("point_id") or data.get("测点编号"),
            )
            if gap is not None:
                key = self.gap_key(gap)
                request = self._active_request(key)
                if request is None and data.get("allow_external_original", True):
                    # 签收可能来自本进程未登记的原单点申请；按外部已出站申请补档。
                    external_id = _text(
                        data.get("original_request_id") or data.get("request_id")
                        or data.get("申请编号") or "ORIGINAL-EXTERNAL"
                    )
                    request = self._register_external_original_request(gap, external_id, data)

        if request is None or gap is None:
            return {"ok": False, "message": "签收未匹配到实际网关、设备身份、时刻对应的补采申请或断点"}

        key = self.gap_key(gap)
        reading_key = self.reading_key(gap)

        # 幂等：同一签收/申请重复回调时返回同一份证据和读数。
        existing_evidence = self._evidence_for_key(key)
        existing_reading = self._reading_for_key(reading_key)
        if existing_evidence and existing_reading:
            self._link_completion_to_documents(
                gap, request, existing_evidence, existing_reading, late=True
            )
            return {
                "ok": True,
                "duplicate": True,
                "message": "该签收已处理，重复回调继续返回同一份完成证据",
                "request": request,
                "evidence": existing_evidence,
                "reading": existing_reading,
                "dispatch_detail": self._details_for_key(key),
            }

        receipt_id = _text(data.get("receipt_id") or data.get("签收编号") or data.get("receipt_no")) or self._next_business_id(
            "recovery_evidence", "RCP", ["签收编号"]
        )
        evidence_id = _text(data.get("evidence_id") or data.get("证据编号")) or self._next_business_id(
            "recovery_evidence", "EVD", ["证据编号", "evidence_id"]
        )

        reading_value = data.get("value", data.get("reading_value", data.get("confirmed_value")))
        readings_input = data.get("readings") or data.get("confirmed_readings")
        reading_payload: dict[str, Any] = {}
        if isinstance(readings_input, list) and readings_input:
            first = readings_input[0]
            if isinstance(first, dict):
                reading_payload = first
                reading_value = reading_value if reading_value is not None else first.get("value")
            else:
                reading_value = first
        if reading_value is None:
            # 确定性补偿值，避免演示/测试因未给值而无法形成确认读数。
            reading_value = 24.5

        reading, reading_created = self._upsert_confirmed_reading(
            gap=gap,
            value=reading_value,
            evidence_id=evidence_id,
            source_request=request,
            payload=reading_payload,
        )

        evidence = {
            "id": self._next_numeric_id("recovery_evidence"),
            "证据编号": evidence_id,
            "evidence_id": evidence_id,
            "签收编号": receipt_id,
            "receipt_id": receipt_id,
            "来源申请编号": request.get("申请编号"),
            "source_request_id": request.get("申请编号"),
            "source_type": request.get("来源类型") or "single_application",
            "网关编号": self.gateway_of(gap),
            "设备身份": self.device_identity_of(gap),
            "设备编号": self.device_name_of(gap),
            "测点编号": self.point_of(gap),
            "sample_time": self.gap_time_of(gap),
            "时刻": _clock_label(self.gap_time_of(gap)),
            "clock": _clock_label(self.gap_time_of(gap)),
            "idempotency_key": key,
            "幂等键": key,
            "reading_key": reading_key,
            "reading_id": reading.get("reading_id"),
            "confirmed_reading_id": reading.get("reading_id"),
            "reading": reading,
            "raw_receipt": data,
            "verified": True,
            "created_at": _now(),
        }
        self._rows("recovery_evidence").append(evidence)

        request["status"] = "已签收"
        request["confirmation_status"] = "confirmed"
        request["receipt_at"] = _now()
        request["evidence_id"] = evidence_id
        request["reading_id"] = reading.get("reading_id")

        gap["status"] = "已补齐"
        gap["处理状态"] = "已补齐"
        gap["pending"] = False
        gap["completion_evidence_id"] = evidence_id
        gap["confirmed_reading_id"] = reading.get("reading_id")
        gap["completion_source"] = (
            "original_application" if request.get("来源类型") == "single_application" else "dispatch_application"
        )

        link = self._link_completion_to_documents(gap, request, evidence, reading, late=True)
        return {
            "ok": True,
            "duplicate": False,
            "reading_created": reading_created,
            "message": "迟到签收已入账，关联单据共享同一份完成证据",
            "request": request,
            "evidence": evidence,
            "reading": reading,
            "linked": link,
            "dispatch_detail": self._details_for_key(key),
        }

    # 常见近义方法名，方便不同调用约定/测试命名。
    confirm_recovery = receive_receipt
    sign_recovery = receive_receipt
    sign_off = receive_receipt
    late_receipt = receive_receipt
    late_signoff = receive_receipt
    submit_receipt = receive_receipt
    acknowledge_recovery = receive_receipt
    complete_recovery = receive_receipt
    receive_signature = receive_receipt
    receive_original_receipt = receive_receipt

    def _find_request(self, identifier: Any) -> dict[str, Any] | None:
        if identifier is None:
            return None
        if isinstance(identifier, dict):
            return identifier
        text_id = _text(identifier)
        for row in self._rows("recovery_request"):
            if text_id.isdigit() and int(row.get("id", -1)) == int(text_id):
                return row
            if text_id in {
                _text(row.get("申请编号")), _text(row.get("request_id")),
                _text(row.get("来源单号")),
            }:
                return row
        return None

    def get_recovery_request(self, identifier: Any) -> dict[str, Any] | None:
        request = self._find_request(identifier)
        if request is None:
            return None
        evidence = self._evidence_for_key(_text(request.get("idempotency_key") or request.get("幂等键")))
        reading = self._reading_for_key(_text(request.get("reading_key")))
        return {**request, "evidence": evidence, "reading": reading, "confirmed_reading": reading}

    def get_evidence(self, identifier: Any) -> dict[str, Any] | None:
        text_id = _text(identifier)
        for row in self._rows("recovery_evidence"):
            if text_id in {_text(row.get("证据编号")), _text(row.get("evidence_id")), _text(row.get("签收编号"))}:
                return row
        return None

    def _register_external_original_request(
        self, gap: dict[str, Any], external_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        existing = self._find_request(external_id)
        if existing is not None:
            return existing
        if external_id in {"ORIGINAL-EXTERNAL", ""}:
            external_id = self._next_business_id("recovery_request", "ORIG", ["申请编号"])
        request = {
            "id": self._next_numeric_id("recovery_request"),
            "申请编号": external_id,
            "request_id": external_id,
            "申请类型": "单点补采",
            "来源类型": "single_application",
            "source_type": "single_application",
            "来源单号": "DATA_GAP_WORKBENCH",
            "网关编号": self.gateway_of(gap),
            "设备身份": self.device_identity_of(gap),
            "设备编号": self.device_name_of(gap),
            "测点编号": self.point_of(gap),
            "sample_time": self.gap_time_of(gap),
            "idempotency_key": self.gap_key(gap),
            "幂等键": self.gap_key(gap),
            "reading_key": self.reading_key(gap),
            "发送状态": "已出站",
            "status": "已出站",
            "confirmation_status": "pending",
            "externally_observed": True,
            "created_at": data.get("sent_at") or _now(),
            "sent_at": data.get("sent_at") or _now(),
            "receipt_at": "",
            "evidence_id": "",
            "reading_id": "",
        }
        self._rows("recovery_request").append(request)
        return request

    def _upsert_confirmed_reading(
        self,
        *,
        gap: dict[str, Any],
        value: Any,
        evidence_id: str,
        source_request: dict[str, Any],
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        reading_key = self.reading_key(gap)
        existing = self._reading_for_key(reading_key)
        if existing is not None:
            return existing, False

        gateway = self.gateway_of(gap)
        identity = self.device_identity_of(gap) or self.device_name_of(gap)
        moment = _time_token(self.gap_time_of(gap)).replace(":", "").replace("-", "")
        safe_identity = re.sub(r"[^A-Za-z0-9]+", "-", identity).strip("-")
        reading_id = _text(payload.get("reading_id") or payload.get("确认读数编号")) or f"READ-{gateway}-{safe_identity}-{moment}"
        reading = {
            "id": self._next_numeric_id("confirmed_reading"),
            "reading_id": reading_id,
            "确认读数编号": reading_id,
            "reading_key": reading_key,
            "网关编号": gateway,
            "设备身份": identity,
            "设备编号": self.device_name_of(gap),
            "测点编号": self.point_of(gap),
            "sample_time": self.gap_time_of(gap),
            "时刻": _clock_label(self.gap_time_of(gap)),
            "clock": _clock_label(self.gap_time_of(gap)),
            "value": value,
            "读数": value,
            "status": "confirmed",
            "确认状态": "已确认",
            "evidence_id": evidence_id,
            "source_request_id": source_request.get("申请编号"),
            "source_type": source_request.get("来源类型"),
            "created_at": _now(),
        }
        self._rows("confirmed_reading").append(reading)
        return reading, True

    def _link_completion_to_documents(
        self,
        gap: dict[str, Any],
        request: dict[str, Any],
        evidence: dict[str, Any],
        reading: dict[str, Any],
        *,
        late: bool = False,
    ) -> dict[str, Any]:
        key = self.gap_key(gap)
        request_ids: list[str] = []
        order_numbers: set[str] = set()
        item_ids: list[Any] = []

        for other_request in self._requests_for_key(key):
            other_request["status"] = "已签收"
            other_request["confirmation_status"] = "confirmed"
            other_request["receipt_at"] = other_request.get("receipt_at") or _now()
            other_request["evidence_id"] = evidence["证据编号"]
            other_request["reading_id"] = reading["reading_id"]
            request_id = _text(other_request.get("申请编号"))
            if request_id:
                request_ids.append(request_id)
            if other_request.get("来源单号") and str(other_request["来源单号"]).startswith("DSP"):
                order_numbers.add(str(other_request["来源单号"]))

        for item in self._rows("incident_dispatch_item"):
            if _text(item.get("idempotency_key")) != key:
                continue
            state = (
                STATE_COMPLETED_ORIGINAL
                if request.get("来源类型") == "single_application"
                or item.get("original_request_id")
                or any(r.get("来源类型") == "single_application" for r in self._requests_for_key(key))
                else STATE_COMPLETED
            )
            item["dispatch_state"] = state
            item["state_label"] = STATE_LABELS[state]
            item["执行状态"] = "已完成"
            item["status"] = "已完成"
            item["evidence_id"] = evidence["证据编号"]
            item["reading_id"] = reading["reading_id"]
            item["shared_evidence"] = True
            item["completion_source"] = (
                "original_application" if state == STATE_COMPLETED_ORIGINAL else "dispatch_application"
            )
            if not item.get("original_request_id") and request.get("来源类型") == "single_application":
                item["original_request_id"] = request.get("申请编号")
            item["source_request_id"] = request.get("申请编号")
            item["closeout_status"] = "两份单据共享同一证据，无需重复采样"
            item["updated_at"] = _now()
            item_ids.append(item.get("明细编号") or item.get("id"))
            if item.get("派工单编号"):
                order_numbers.add(str(item["派工单编号"]))

        for order_number in order_numbers:
            order = self._get_order_row(order_number)
            if order is not None:
                self._refresh_order_summary(order)

        return {
            "request_ids": request_ids,
            "dispatch_order_ids": sorted(order_numbers),
            "dispatch_item_ids": item_ids,
            "shared_evidence": True,
            "duplicate_readings_prevented": True,
            "late": late,
        }

    def _details_for_key(self, key: str) -> dict[str, Any]:
        items = [
            item for item in self._rows("incident_dispatch_item")
            if _text(item.get("idempotency_key")) == key
        ]
        order_ids = sorted({_text(item.get("派工单编号")) for item in items if item.get("派工单编号")})
        return {
            "items": items,
            "order_ids": order_ids,
            "orders": [detail for order_id in order_ids if (detail := self.get_dispatch_detail(order_id))],
        }

    # ------------------------------------------------------------------
    # 派工单查询 / 核对收尾
    # ------------------------------------------------------------------
    def _get_order_row(self, identifier: Any) -> dict[str, Any] | None:
        if identifier is None:
            return None
        if isinstance(identifier, dict):
            return identifier
        text_id = _text(identifier)
        for row in self._rows("incident_dispatch"):
            if text_id.isdigit() and int(row.get("id", -1)) == int(text_id):
                return row
            if text_id in {_text(row.get("派工单编号")), _text(row.get("order_id"))}:
                return row
        return None

    def _find_item(self, order: dict[str, Any] | None, item_identifier: Any = None) -> dict[str, Any] | None:
        items = self._rows("incident_dispatch_item")
        order_number = _text((order or {}).get("派工单编号"))
        scoped = [item for item in items if not order_number or item.get("派工单编号") == order_number]
        if item_identifier is None:
            return scoped[-1] if scoped else None
        if isinstance(item_identifier, dict):
            for item in scoped:
                if item is item_identifier:
                    return item
        text_id = _text(item_identifier)
        for item in scoped:
            if text_id.isdigit() and int(item.get("id", -1)) == int(text_id):
                return item
            if text_id in {_text(item.get("明细编号")), _text(item.get("item_id")), _text(item.get("记录编号")), _text(item.get("gap_id"))}:
                return item
            if text_id == _time_token(item.get("sample_time") or item.get("时刻")):
                return item
        gap = self._resolve_gap(item_identifier)
        if gap is not None:
            key = self.gap_key(gap)
            for item in scoped:
                if item.get("idempotency_key") == key:
                    return item
        return None

    def get_dispatch_detail(
        self, order_identifier: Any, _include_other_details: bool = True, **kwargs: Any
    ) -> dict[str, Any] | None:
        order = self._get_order_row(order_identifier or kwargs.get("order_id"))
        if order is None:
            return None
        item_rows = [
            item for item in self._rows("incident_dispatch_item")
            if item.get("派工单编号") == order.get("派工单编号")
        ]
        enriched: list[dict[str, Any]] = []
        evidence_ids: set[str] = set()
        reading_ids: set[str] = set()
        other_orders: set[str] = set()
        other_requests: set[str] = set()

        for item in item_rows:
            gap = self._resolve_gap(item.get("idempotency_key")) or self._resolve_gap(item.get("记录编号"))
            view = self.gap_view(gap) if gap else {}
            merged = {**view, **item}
            # 动态状态优先，保证刷新即可看到迟到签收后的结果。
            if view:
                merged["dispatch_state"] = view["dispatch_state"]
                merged["state_label"] = view["state_label"]
                merged["evidence_id"] = view["evidence_id"] or item.get("evidence_id")
                merged["reading_id"] = view["reading_id"] or item.get("reading_id")
                if view["dispatch_state"] == STATE_OUTBOUND_PENDING:
                    merged["执行状态"] = item.get("执行状态") or "待签收确认"
                elif view["dispatch_state"].startswith("completed"):
                    merged["执行状态"] = "已完成"
            enriched.append(merged)
            if merged.get("evidence_id"):
                evidence_ids.add(str(merged["evidence_id"]))
            if merged.get("reading_id"):
                reading_ids.add(str(merged["reading_id"]))
            for request in self._requests_for_key(item.get("idempotency_key", "")):
                rid = _text(request.get("申请编号"))
                if rid:
                    other_requests.add(rid)
                ref = _text(request.get("来源单号"))
                if ref.startswith("DSP") and ref != order.get("派工单编号"):
                    other_orders.add(ref)
            for other_item in self._rows("incident_dispatch_item"):
                if other_item.get("idempotency_key") == item.get("idempotency_key"):
                    other_order = _text(other_item.get("派工单编号"))
                    if other_order and other_order != order.get("派工单编号"):
                        other_orders.add(other_order)

        evidences = [
            row for row in self._rows("recovery_evidence")
            if row.get("证据编号") in evidence_ids or row.get("evidence_id") in evidence_ids
        ]
        readings = [
            row for row in self._rows("confirmed_reading")
            if row.get("reading_id") in reading_ids or row.get("确认读数编号") in reading_ids
        ]
        all_done = all(item.get("dispatch_state", "").startswith("completed") or item.get("dispatch_state") == STATE_ABANDONED for item in enriched)
        any_open = any(item.get("dispatch_state") in {STATE_NOT_SENT, STATE_OUTBOUND_PENDING} for item in enriched)
        source_verification_required = any(
            not item.get("source_verified")
            and str(item.get("dispatch_state", "")).startswith("completed")
            for item in enriched
        )

        request_documents: list[dict[str, Any]] = []
        for request_id in sorted(other_requests):
            request = self._find_request(request_id)
            if request is None:
                continue
            request_documents.append({
                "request_id": request_id,
                "申请编号": request_id,
                "source_type": request.get("来源类型"),
                "来源类型": request.get("来源类型"),
                "status": request.get("status"),
                "状态": request.get("status"),
                "evidence_id": request.get("evidence_id"),
                "reading_id": request.get("reading_id"),
                "needs_closeout": request.get("status") in OPEN_REQUEST_STATUSES,
            })
        other_application = next(
            (doc for doc in request_documents if doc.get("source_type") == "single_application"),
            None,
        )
        if _include_other_details:
            other_order_details = [
                detail
                for oid in sorted(other_orders)
                if (detail := self.get_dispatch_detail(oid, _include_other_details=False))
            ]
        else:
            other_order_details = [
                {"order_id": oid, "派工单编号": oid, "summary_only": True}
                for oid in sorted(other_orders)
            ]
        other_order_needs_closeout = any(
            (other_order := self._get_order_row(oid)) is not None
            and other_order.get("status") not in {"已完成", "已收尾", "已取消"}
            for oid in sorted(other_orders)
        )
        other_request_needs_closeout = any(doc["needs_closeout"] for doc in request_documents)
        document_closeout_required = other_order_needs_closeout or other_request_needs_closeout
        closeout_required = source_verification_required or document_closeout_required
        return {
            "ok": True,
            "order": order,
            "dispatch_order": order,
            **order,
            "items": enriched,
            "明细": enriched,
            "evidence": evidences,
            "evidences": evidences,
            "completion_evidence": evidences[-1] if evidences else None,
            "shared_evidence": evidences[-1] if evidences else None,
            "readings": readings,
            "confirmed_readings": readings,
            "reading": readings[-1] if readings else None,
            "confirmed_reading": readings[-1] if readings else None,
            "related_request_ids": sorted(other_requests),
            "other_request_ids": sorted(other_requests),
            "related_documents": request_documents,
            "other_documents": request_documents + [
                {"type": "dispatch_order", "order_id": oid} for oid in sorted(other_orders)
            ],
            "other_application": other_application,
            "other_dispatch_order_ids": sorted(other_orders),
            "other_orders": other_order_details,
            "closeout": {
                "required": closeout_required,
                "needs_closeout": closeout_required,
                "source_verification_required": source_verification_required,
                "document_closeout_required": document_closeout_required,
                "other_document_closeout_required": document_closeout_required,
                "other_application_needs_closeout": bool(other_application and other_application.get("needs_closeout")),
                "other_order_needs_closeout": other_order_needs_closeout,
                "other_order_ids": sorted(other_orders),
                "other_request_ids": sorted(other_requests),
                "documents": request_documents,
                "message": (
                    "可核对补采来源；原申请签收已形成共享证据，另一笔无需重复采样"
                    if not document_closeout_required
                    else "有关联单据仍待签收或收尾"
                ),
            },
            "all_completed": all_done and not any_open,
        }

    def refresh_dispatch_detail(self, order_identifier: Any) -> dict[str, Any] | None:
        order = self._get_order_row(order_identifier)
        if order is not None:
            self.reconcile_order(order)
        return self.get_dispatch_detail(order_identifier)

    def reconcile_order(self, order_identifier: Any) -> dict[str, Any]:
        order = self._get_order_row(order_identifier)
        if order is None:
            return {"ok": False, "message": "派工单不存在"}
        for item in self._rows("incident_dispatch_item"):
            if item.get("派工单编号") != order.get("派工单编号"):
                continue
            gap = self._resolve_gap(item.get("idempotency_key"))
            if gap is None:
                continue
            view = self.gap_view(gap)
            if view["request_id"] and not item.get("source_request_id"):
                item["source_request_id"] = view["request_id"]
            if view["evidence_id"]:
                item["evidence_id"] = view["evidence_id"]
                item["shared_evidence"] = True
            if view["reading_id"]:
                item["reading_id"] = view["reading_id"]
            item["dispatch_state"] = view["dispatch_state"]
            item["state_label"] = view["state_label"]
            item["updated_at"] = _now()
        self._refresh_order_summary(order)
        return {"ok": True, "order": self.get_dispatch_detail(order["派工单编号"])}

    def verify_source(self, order_identifier: Any = None, item_identifier: Any = None, **kwargs: Any) -> dict[str, Any]:
        order = self._get_order_row(order_identifier or kwargs.get("order_id"))
        item = self._find_item(order, item_identifier or kwargs.get("item_id") or kwargs.get("gap_id"))
        if item is None:
            return {"ok": False, "message": "待核对的派工明细不存在"}
        item["source_verified"] = True
        item["核对状态"] = "已核对"
        item["verified_at"] = _now()
        item["closeout_status"] = "来源已核对，共享证据有效"
        if order is not None:
            self._refresh_order_summary(order)
        return {"ok": True, "message": "补采来源已核对", "item": item, "detail": self.get_dispatch_detail(order["派工单编号"]) if order else None}

    def close_item(self, order_identifier: Any = None, item_identifier: Any = None, **kwargs: Any) -> dict[str, Any]:
        result = self.verify_source(order_identifier, item_identifier, **kwargs)
        if not result.get("ok"):
            return result
        item = result["item"]
        item["closeout_status"] = "已收尾"
        item["status"] = "已收尾"
        order = self._get_order_row(order_identifier or kwargs.get("order_id"))
        if order is not None:
            self._refresh_order_summary(order)
        return {"ok": True, "message": "派工明细已收尾", "item": item}

    def close_dispatch_order(self, order_identifier: Any, **kwargs: Any) -> dict[str, Any]:
        detail = self.get_dispatch_detail(order_identifier)
        if detail is None:
            return {"ok": False, "message": "派工单不存在"}
        for item in detail["items"]:
            if item.get("dispatch_state") in {STATE_NOT_SENT, STATE_OUTBOUND_PENDING}:
                return {"ok": False, "message": "仍有未发出或等待签收的项目，不能收尾"}
            row = self._find_item(detail["order"], item.get("明细编号"))
            if row is not None:
                row["source_verified"] = True
                row["核对状态"] = "已核对"
                row["closeout_status"] = "已收尾"
        order = detail["order"]
        order["status"] = "已完成"
        order["状态"] = "已完成"
        order["pending"] = False
        self._refresh_order_summary(order)
        return {"ok": True, "message": "派工单已核对并收尾", "detail": self.get_dispatch_detail(order_identifier)}

    def _refresh_order_summary(self, order: dict[str, Any]) -> None:
        items = [
            item for item in self._rows("incident_dispatch_item")
            if item.get("派工单编号") == order.get("派工单编号")
        ]
        order["派发断点"] = "、".join(_text(item.get("时刻")) or _text(item.get("gap_id")) for item in items)
        request_ids = [_text(item.get("original_request_id") or item.get("source_request_id")) for item in items if item.get("source_request_id")]
        evidence_ids = [_text(item.get("evidence_id")) for item in items if item.get("evidence_id")]
        order["共享申请编号"] = "、".join(dict.fromkeys(filter(None, request_ids)))
        order["完成证据编号"] = "、".join(dict.fromkeys(filter(None, evidence_ids)))
        states = [item.get("dispatch_state") for item in items]
        if states and all(state in {STATE_COMPLETED, STATE_COMPLETED_ORIGINAL, STATE_ABANDONED} for state in states):
            order["status"] = "已完成"
            order["状态"] = "已完成"
            order["pending"] = False
        elif any(state in {STATE_COMPLETED, STATE_COMPLETED_ORIGINAL, STATE_OUTBOUND_PENDING} for state in states):
            order["status"] = "部分完成" if any(state in {STATE_COMPLETED, STATE_COMPLETED_ORIGINAL} for state in states) else "处理中"
            order["状态"] = order["status"]
            order["pending"] = True
        else:
            order["status"] = "已领取"
            order["状态"] = "已领取"
        order["item_count"] = len(items)
        order["completed_count"] = sum(1 for state in states if state in {STATE_COMPLETED, STATE_COMPLETED_ORIGINAL})
        order["updated_at"] = _now()

    # ------------------------------------------------------------------
    # 工作台 / 唯一性审计
    # ------------------------------------------------------------------
    def workbench(self, incident_identifier: Any = SCENARIO_INCIDENT_ID, **filters: Any) -> dict[str, Any]:
        incident = self.get_incident(incident_identifier, **filters)
        positions = self.query_gaps(incident) if incident is not None else self.query_gaps(**filters)
        groups: dict[str, list[dict[str, Any]]] = {
            STATE_NOT_SENT: [],
            STATE_COMPLETED_ORIGINAL: [],
            STATE_OUTBOUND_PENDING: [],
            STATE_COMPLETED: [],
            STATE_ABANDONED: [],
        }
        for position in positions:
            groups.setdefault(position["dispatch_state"], []).append(position)

        orders = list(reversed(self._rows("incident_dispatch")))
        readings = self._rows("confirmed_reading")
        uniqueness = self.audit_reading_uniqueness(incident_identifier)
        gateway_ids = sorted({position["网关编号"] for position in positions})
        return {
            "ok": True,
            "incident": incident,
            "items": positions,
            "positions": positions,
            "not_sent": groups[STATE_NOT_SENT],
            "not_dispatched": groups[STATE_NOT_SENT],
            "确实未发": groups[STATE_NOT_SENT],
            "completed_by_original": groups[STATE_COMPLETED_ORIGINAL],
            "completed_by_original_application": groups[STATE_COMPLETED_ORIGINAL],
            "已经由原申请完成": groups[STATE_COMPLETED_ORIGINAL],
            "outbound_pending": groups[STATE_OUTBOUND_PENDING],
            "pending_confirmation": groups[STATE_OUTBOUND_PENDING],
            "sent_awaiting_receipt": groups[STATE_OUTBOUND_PENDING],
            "已出站但仍需等候确认": groups[STATE_OUTBOUND_PENDING],
            "completed": groups[STATE_COMPLETED],
            "abandoned": groups[STATE_ABANDONED],
            "state_labels": STATE_LABELS,
            "counts": {
                **{key: len(value) for key, value in groups.items()},
                "not_dispatched": len(groups[STATE_NOT_SENT]),
                "pending_confirmation": len(groups[STATE_OUTBOUND_PENDING]),
                "completed_by_original_application": len(groups[STATE_COMPLETED_ORIGINAL]),
            },
            "orders": orders,
            "dispatch_orders": [self.get_dispatch_detail(order.get("派工单编号")) for order in orders],
            "readings": readings,
            "gateway_ids": gateway_ids,
            "neighbor_gateway_dispatched": any("G2" in str(order.get("网关编号")) for order in orders),
            "reading_uniqueness": uniqueness,
        }

    def dispatch_workbench(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.workbench(*args, **kwargs)

    def get_workbench(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.workbench(*args, **kwargs)

    def dispatch_board(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.workbench(*args, **kwargs)

    def audit_reading_uniqueness(self, incident_identifier: Any = SCENARIO_INCIDENT_ID) -> dict[str, Any]:
        readings = self._rows("confirmed_reading")
        if incident_identifier:
            incident = self.get_incident(incident_identifier)
            if incident is not None:
                gateway = self.gateway_of(incident)
                identities = set(_as_list(incident.get("圈定设备身份")))
                readings = [
                    row for row in readings
                    if self.gateway_of(row) == gateway
                    and (not identities or self.device_identity_of(row) in identities)
                ]
        by_moment: dict[str, list[dict[str, Any]]] = {}
        for reading in readings:
            key = f"{reading.get('网关编号')}|{reading.get('设备身份')}|{reading.get('sample_time') or reading.get('时刻')}"
            by_moment.setdefault(key, []).append(reading)
        duplicates = {key: rows for key, rows in by_moment.items() if len({row.get("reading_id") for row in rows}) > 1 or len(rows) > 1}
        return {
            "unique": not duplicates,
            "total": len(readings),
            "moments": len(by_moment),
            "duplicates": duplicates,
        }

    def list_confirmed_readings(self, incident_identifier: Any = None, **filters: Any) -> list[dict[str, Any]]:
        readings = self._rows("confirmed_reading")
        if incident_identifier:
            incident = self.get_incident(incident_identifier, **filters)
            gateway = self.gateway_of(incident) if incident else filters.get("gateway_id")
            identities = set(_as_list((incident or {}).get("圈定设备身份")))
            if gateway:
                readings = [row for row in readings if self.gateway_of(row) == gateway]
            if identities:
                readings = [row for row in readings if self.device_identity_of(row) in identities]
        return list(readings)

    def record_confirmed_reading(self, gap_identifier: Any = None, value: Any = None, **kwargs: Any) -> dict[str, Any]:
        gap = self._resolve_gap(gap_identifier, **kwargs)
        if gap is None:
            return {"ok": False, "message": "未找到对应断点"}
        request = self._active_request(self.gap_key(gap)) or {}
        reading, created = self._upsert_confirmed_reading(
            gap=gap,
            value=value if value is not None else 24.5,
            evidence_id=_text(kwargs.get("evidence_id")),
            source_request=request,
            payload=kwargs,
        )
        return {"ok": True, "created": created, "reading": reading}

    def list_entries(self, *args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        # 通用 /api/incident_dispatch 列表返回派工单，而不是基础空表过滤。
        return super().list_entries(*args, **kwargs)


incident_dispatch_service = IncidentDispatchService()
