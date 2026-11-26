"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.device_registry import DeviceRegistryService
from app.services.protocol_cfg import ProtocolCfgService
from app.services.msg_decode import MsgDecodeService
from app.services.msg_router import MsgRouterService
from app.services.ts_point import TsPointService
from app.services.ts_store import TsStoreService
from app.services.downsample import DownsampleService
from app.services.alarm_trigger import AlarmTriggerService
from app.services.firmware_ota import FirmwareOtaService
from app.services.gateway_mgmt import GatewayMgmtService
from app.services.gateway_incident import GatewayIncidentService
from app.services.shadow_dev import ShadowDevService
from app.services.data_gap import DataGapService
from app.services.incident_dispatch import IncidentDispatchService
from app.services.recovery_dispatch import RecoveryDispatchService
from app.services.gap_dispatch import GapDispatchService
from app.services.gap_recovery import GapRecoveryService
from app.services.batch_export import BatchExportService
from app.services.rule_engine import RuleEngineService
from app.services.edge_cache import EdgeCacheService
from app.services.tag_binding import TagBindingService
from app.services.data_stream import DataStreamService
from app.services.aggregate_sink import AggregateSinkService

SERVICES: dict[str, BaseModuleService] = {
    "device_registry": DeviceRegistryService(),
    "protocol_cfg": ProtocolCfgService(),
    "msg_decode": MsgDecodeService(),
    "msg_router": MsgRouterService(),
    "ts_point": TsPointService(),
    "ts_store": TsStoreService(),
    "downsample": DownsampleService(),
    "alarm_trigger": AlarmTriggerService(),
    "firmware_ota": FirmwareOtaService(),
    "gateway_mgmt": GatewayMgmtService(),
    "gateway_incident": GatewayIncidentService(),
    "shadow_dev": ShadowDevService(),
    "data_gap": DataGapService(),
    "incident_dispatch": IncidentDispatchService(),
    "recovery_dispatch": RecoveryDispatchService(),
    "gap_dispatch": GapDispatchService(),
    "gap_recovery": GapRecoveryService(),
    "batch_export": BatchExportService(),
    "rule_engine": RuleEngineService(),
    "edge_cache": EdgeCacheService(),
    "tag_binding": TagBindingService(),
    "data_stream": DataStreamService(),
    "aggregate_sink": AggregateSinkService(),
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
