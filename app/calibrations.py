"""设备指标的线性校准配置：生效区间管理与按时刻解析。

校准模型为线性仿射变换：``calibrated = raw * scale + offset``，
其中 offset / scale 按 (设备, 指标) 配置，并带有一个半开有效期
``[valid_from, valid_to)``；``valid_to`` 为 null 表示长期有效（开放终点）。

时间语义：
- 入参的 naive datetime 一律按 UTC 处理，带时区的先转换到 UTC，
  再以固定 6 位小数秒 + ``Z`` 落库，该文本可直接做字典序比较；
- 同一 (设备, 指标) 的有效期**不得重叠**，端点相接允许
  （前段 ``valid_to`` == 后段 ``valid_from`` 合法）；
- 按 ``at`` 解析时，命中条件为 ``valid_from <= at < valid_to``，
  由于区间互不重叠，命中结果至多一条。

校准只在读取解析时计算，**绝不改写已提交的原始测量值**。
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# 开放终点在比较时的哨兵：公元 9999 年，字典序晚于任何真实有效时间
_OPEN_ENDED = "9999-01-01T00:00:00.000000Z"


class CalibrationError(ValueError):
    """校准配置参数非法（如 valid_from 不早于 valid_to）。"""


class CalibrationConflict(Exception):
    """同设备同指标的校准配置有效期与既有配置重叠。"""


@dataclass(frozen=True)
class Calibration:
    id: int
    device_id: str
    metric: str
    valid_from: str
    valid_to: Optional[str]
    offset: float
    scale: float
    created_at: str


def to_utc_text(dt: datetime) -> str:
    """归一化为 UTC、固定 6 位小数秒、以 Z 结尾的可字典序比较文本。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="microseconds")
        + "Z"
    )


def _row_to_calibration(row: sqlite3.Row) -> Calibration:
    return Calibration(
        id=row["id"],
        device_id=row["device_id"],
        metric=row["metric"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        offset=row["offset"],
        scale=row["scale"],
        created_at=row["created_at"],
    )


def create_calibration(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    valid_from: datetime,
    valid_to: Optional[datetime],
    offset: float,
    scale: float,
) -> Calibration:
    """在同一事务内校验区间不重叠并写入校准配置。

    依赖区间互不重叠的不变量；若解析出多条生效配置则属内部错误。
    """
    from_text = to_utc_text(valid_from)
    to_text = to_utc_text(valid_to) if valid_to is not None else None
    if to_text is not None and from_text >= to_text:
        raise CalibrationError("valid_from 必须早于 valid_to")

    # 半开区间重叠判定：既有.from < 新.to 且 新.from < 既有.to
    # （开放终点以晚于一切真实时间的哨兵代替），端点相接不算重叠。
    overlap = conn.execute(
        """
        SELECT id FROM calibrations
         WHERE device_id = ?
           AND metric = ?
           AND valid_from < COALESCE(?, ?)
           AND ? < COALESCE(valid_to, ?)
         LIMIT 1
        """,
        (device_id, metric, to_text, _OPEN_ENDED, from_text, _OPEN_ENDED),
    ).fetchone()
    if overlap is not None:
        raise CalibrationConflict(
            f"设备 {device_id} 指标 {metric} 已存在与该有效期重叠的校准配置 "
            f"(id={overlap['id']})"
        )

    cur = conn.execute(
        """
        INSERT INTO calibrations
            (device_id, metric, valid_from, valid_to, offset, scale)
        VALUES (?,?,?,?,?,?)
        """,
        (device_id, metric, from_text, to_text, offset, scale),
    )
    row = conn.execute(
        "SELECT * FROM calibrations WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return _row_to_calibration(row)


def resolve_calibration(
    conn: sqlite3.Connection,
    device_id: str,
    metric: str,
    at: datetime,
) -> Optional[Calibration]:
    """返回 at 时刻对 (设备, 指标) 唯一生效的校准配置；无配置返回 None。"""
    at_text = to_utc_text(at)
    rows = conn.execute(
        """
        SELECT * FROM calibrations
         WHERE device_id = ?
           AND metric = ?
           AND valid_from <= ?
           AND (valid_to IS NULL OR ? < valid_to)
         ORDER BY valid_from
        """,
        (device_id, metric, at_text, at_text),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # 创建路径在事务内保证不重叠；出现多条说明不变量被破坏
        raise CalibrationError(
            f"设备 {device_id} 指标 {metric} 在 {at_text} 存在多条生效校准配置"
        )
    return _row_to_calibration(rows[0])


def apply(calibration: Calibration, value: float) -> float:
    """线性校准：calibrated = raw * scale + offset。"""
    return value * calibration.scale + calibration.offset
