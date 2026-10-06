"""定义农业试验裁定服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvaluationResult:
    """描述一次试验集合评估的完整结果，可被冻结为分析版本。"""

    protocol_id: str
    region_id: str
    variety_id: str
    valid: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class IngestOutcome:
    """描述一批离线观测记录归并后的分类结果。"""

    request_id: str
    trial_id: str
    device_id: str
    accepted: int
    pending: int
    duplicates: int
    items: list[dict[str, Any]]
    replayed: bool
