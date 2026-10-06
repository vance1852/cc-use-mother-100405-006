"""农业试验裁定的领域规则：谓词评估、校准/气象/时间窗、结论计算。

排除规则的 expression 以 JSON 谓词树存储，避免执行任意代码：

    {"all": [{"op": ">", "field": "value", "value": 0.35},
             {"op": "==", "field": "metric", "value": "disease_rate"}]}

支持 all/any/not 与比较运算；field 从评估上下文中按点号取值。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable


# 观测进入待审（而非直接丢弃）的原因
PENDING_OUT_OF_ORDER = "out_of_order"        # 越序：观测时间早于该地块同指标上一有效观测
PENDING_OUT_OF_WINDOW = "out_of_window"      # 超窗：落在方案允许窗口之外
PENDING_CALIBRATION_EXPIRED = "calibration_expired"  # 校准时无有效校准
PENDING_OFFLINE_LATE = "offline_late"        # 离线补录且晚于接收入口允许的迟到宽限
PENDING_PLOT_WEATHER_DISASTER = "plot_weather_diversion"  # 灾害性气象使地块偏离原方案

VALID_DECISIONS = ("recommend", "restrict", "reject")

# 农事操作到方案时间窗字段的映射
WINDOW_BY_OPERATION = {
    "sowing": ("sow_from", "sow_to"),
    "harvest": ("harvest_from", "harvest_to"),
}
WINDOW_BY_METRIC = {
    "seedling_rate": ("observe_from", "observe_to"),
    "yield": ("harvest_from", "harvest_to"),
    "disease_rate": ("observe_from", "observe_to"),
}


def parse_instant(value: str) -> datetime:
    """把 ISO-8601 文本解析成可比较的 datetime。"""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


def _resolve(field: str, context: dict[str, Any]) -> Any:
    current: Any = context
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def evaluate_predicate(node: Any, context: dict[str, Any]) -> bool:
    """递归评估一棵谓词树。"""

    if not isinstance(node, dict):
        raise ValueError("谓词必须是对象")
    if "all" in node:
        return all(evaluate_predicate(child, context) for child in node["all"])
    if "any" in node:
        return any(evaluate_predicate(child, context) for child in node["any"])
    if "not" in node:
        return not evaluate_predicate(node["not"], context)
    operator = node.get("op")
    field = node.get("field")
    expected = node.get("value")
    if not operator or not isinstance(field, str):
        raise ValueError("谓词缺少 op 或 field")
    actual = _resolve(field, context)
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator in (">", ">=", "<", "<="):
        if actual is None:
            return False
        if operator == ">":
            return actual > expected
        if operator == ">=":
            return actual >= expected
        if operator == "<":
            return actual < expected
        return actual <= expected
    if operator == "in":
        return actual in expected
    raise ValueError(f"不支持的运算符: {operator}")


def load_expression(expression_text: str) -> Any:
    """解析规则表达式文本（规范 JSON）。"""

    try:
        parsed = json.loads(expression_text)
    except json.JSONDecodeError as exc:
        raise ValueError("规则表达式必须是 JSON 谓词树") from exc
    # 用一次空上下文校验结构
    evaluate_predicate(parsed, {})
    return parsed


def calibration_active(calibrations: Iterable[dict[str, Any]], observed_at: datetime) -> dict[str, Any] | None:
    """返回观测时刻处于有效区间内的校准记录。"""

    for calibration in calibrations:
        valid_from = parse_instant(calibration["valid_from"])
        valid_to_text = calibration.get("valid_to")
        valid_to = parse_instant(valid_to_text) if valid_to_text else None
        if observed_at >= valid_from and (valid_to is None or observed_at < valid_to):
            return calibration
    return None


def weather_overlap(weather_rows: Iterable[dict[str, Any]], started_at: datetime, ended_at: datetime) -> list[dict[str, Any]]:
    """返回与给定区间重叠的气象区间。"""

    overlaps = []
    for row in weather_rows:
        w_start = parse_instant(row["started_at"])
        w_end = parse_instant(row["ended_at"])
        if w_start < ended_at and w_end > started_at:
            overlaps.append(row)
    return overlaps


def in_window(protocol: dict[str, Any], window_key: tuple[str, str], instant: datetime) -> bool:
    """判断时间点是否落在方案规定窗口内（窗口可缺省，缺省表示不限制）。"""

    from_key, to_key = window_key
    from_text = protocol.get(from_key)
    to_text = protocol.get(to_key)
    if from_text is None and to_text is None:
        return True
    if from_text and instant < parse_instant(from_text):
        return False
    if to_text and instant > parse_instant(to_text):
        return False
    return True


def compute_decision(*, effective_plot_count: int, excluded_plot_count: int,
                     mean_yield: float | None, mean_disease: float | None,
                     mean_seedling: float | None, thresholds: dict[str, float]) -> tuple[str, list[str]]:
    """根据有效试验集合与阈值计算推荐/限制/拒绝及理由。

    规则透明可复核：先看有效样本量，再按产量/抗病/成苗指标判定。
    """

    reasons: list[str] = []
    min_plots = thresholds["min_effective_plots"]
    if effective_plot_count < min_plots:
        reasons.append(f"有效地块 {effective_plot_count} 个，低于最低要求 {min_plots} 个（另有 {excluded_plot_count} 个被排除）")
        return "reject", reasons
    reasons.append(f"有效地块 {effective_plot_count} 个，排除地块 {excluded_plot_count} 个")
    if mean_yield is not None:
        reasons.append(f"平均产量指数 {mean_yield:.3f}，阈值 {thresholds['yield_recommend']}/{thresholds['yield_restrict']}")
    else:
        reasons.append("有效试验缺少产量指标，无法证明产量达标")
    if mean_disease is not None:
        reasons.append(f"平均病株率 {mean_disease:.3f}，阈值 {thresholds['disease_restrict']}")
    if mean_seedling is not None:
        reasons.append(f"平均成苗率 {mean_seedling:.3f}，阈值 {thresholds['seedling_restrict']}")

    disease_fail = mean_disease is not None and mean_disease > thresholds["disease_restrict"]
    seedling_fail = mean_seedling is not None and mean_seedling < thresholds["seedling_restrict"]
    yield_low = mean_yield is not None and mean_yield < thresholds["yield_restrict"]

    # 抗病或成苗率硬性不达标，或产量明确低于限制线：拒绝
    if disease_fail or seedling_fail or yield_low:
        return "reject", reasons
    # 产量缺失或仅处于限制与推荐之间：限制推广
    if mean_yield is None or mean_yield < thresholds["yield_recommend"]:
        return "restrict", reasons
    return "recommend", reasons
