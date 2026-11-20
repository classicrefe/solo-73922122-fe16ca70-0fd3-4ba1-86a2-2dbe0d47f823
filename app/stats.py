"""pH × 电导率统计包：CSV 解析、充分统计量与 Pearson 相关系数。

设计要点
--------
* 每个发布序号只保留充分统计量 ``(n, Σx, Σy, Σx², Σy², Σxy)``，
  统计包不保留逐行读数，合并时无需重读原件；
* 测量值至多多少六位小数，统一按 1e6 定标为整数再求和，全程精确、
  无浮点漂移，因此“合并若干包的统计结果”与“去重后对原件直接统计”
  逐位一致（同一组整数和、同一个 Pearson 公式）；
* 合并 = 按发布序号取并集（同一序号只计一次，异单同字节因序号不同
  分别计入），再把充分统计量逐项相加；
* Pearson 系数在有效样本不足两行或任一列恒定（方差为零）时为 ``None``。
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from decimal import Decimal

PH_COLUMN = "ph"
COND_COLUMN = "conductivity_us_cm"

PH_MIN = Decimal("0")
PH_MAX = Decimal("14")
COND_MIN = Decimal("0")
COND_MAX = Decimal("1000000")

# 至多六位小数 -> 乘 1e6 后为精确整数。
SCALE = 1_000_000

# 无符号十进制数：整数，或至多六位小数（允许 ".5" 这类省略前导零的写法）。
_VALUE_RE = re.compile(r"^(?:\d+(?:\.\d{1,6})?|\.\d{1,6})$")

# 单件原件可达 1 MiB，放宽 csv 默认的 128 KiB 字段上限，避免误报格式错误。
csv.field_size_limit(4 * 1024 * 1024)


class CsvFormatError(Exception):
    """原件 CSV 格式错误：编码、表头、数值格式或取值范围不合规。"""


@dataclass(frozen=True)
class ReleaseStats:
    """单个发布序号的充分统计量（整数定标，精确无漂移）。"""

    n: int = 0
    sx: int = 0
    sy: int = 0
    sxx: int = 0
    syy: int = 0
    sxy: int = 0

    def combine(self, other: "ReleaseStats") -> "ReleaseStats":
        """逐项相加（合并两个序号集合的统计量）。"""
        return ReleaseStats(
            n=self.n + other.n,
            sx=self.sx + other.sx,
            sy=self.sy + other.sy,
            sxx=self.sxx + other.sxx,
            syy=self.syy + other.syy,
            sxy=self.sxy + other.sxy,
        )

    def as_row(self) -> tuple:
        """落库用元组（大整数以十进制字符串保存，超出 SQLite 64 位整型）。"""
        return (
            self.n,
            str(self.sx),
            str(self.sy),
            str(self.sxx),
            str(self.syy),
            str(self.sxy),
        )

    @classmethod
    def from_row(cls, row) -> "ReleaseStats":
        return cls(
            n=int(row["n"]),
            sx=int(row["sx"]),
            sy=int(row["sy"]),
            sxx=int(row["sxx"]),
            syy=int(row["syy"]),
            sxy=int(row["sxy"]),
        )


def _parse_measurement(
    raw: str | None, lo: Decimal, hi: Decimal, column: str, line_no: int
) -> int | None:
    """解析单个单元格。空值返回 None；格式或范围不合规抛 CsvFormatError。"""
    text = (raw or "").strip()
    if text == "":
        return None
    if not _VALUE_RE.match(text):
        raise CsvFormatError(
            f"第 {line_no} 行 {column} 列 {text!r} 不是至多六位小数的十进制数"
        )
    value = Decimal(text)
    if not (lo <= value <= hi):
        raise CsvFormatError(
            f"第 {line_no} 行 {column} 列 {text!r} 超出取值范围 [{lo}, {hi}]"
        )
    scaled = value * SCALE
    assert scaled == scaled.to_integral_value()  # 正则已保证至多六位小数
    return int(scaled)


def parse_release_csv(data: bytes) -> ReleaseStats:
    """解析一件 UTF-8 CSV 原件，返回该发布序号的充分统计量。

    仅 ``ph`` 与 ``conductivity_us_cm`` 两列都有值的行参与统计；
    任一非空单元格格式/范围不合规即抛 :class:`CsvFormatError`（整次制包失败）。
    """
    try:
        text = data.decode("utf-8-sig")  # 容忍 BOM，其余编码一律拒绝
    except UnicodeDecodeError as exc:
        raise CsvFormatError(f"原件不是合法 UTF-8 编码：{exc}") from exc

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        rows = iter(reader)
        try:
            header = next(rows)
        except StopIteration:
            raise CsvFormatError("CSV 缺少表头行")
        header = [cell.strip() for cell in header]
        try:
            ph_idx = header.index(PH_COLUMN)
        except ValueError:
            raise CsvFormatError(f"表头缺少必需列 {PH_COLUMN!r}")
        try:
            cond_idx = header.index(COND_COLUMN)
        except ValueError:
            raise CsvFormatError(f"表头缺少必需列 {COND_COLUMN!r}")

        n = sx = sy = sxx = syy = sxy = 0
        for line_no, row in enumerate(rows, start=2):
            # 短行缺失的单元格按空值处理（该行不参与统计）。
            ph_raw = row[ph_idx] if ph_idx < len(row) else ""
            cond_raw = row[cond_idx] if cond_idx < len(row) else ""
            ph = _parse_measurement(ph_raw, PH_MIN, PH_MAX, PH_COLUMN, line_no)
            cond = _parse_measurement(
                cond_raw, COND_MIN, COND_MAX, COND_COLUMN, line_no
            )
            if ph is None or cond is None:
                continue  # 仅两列都有值的行参与统计
            n += 1
            sx += ph
            sy += cond
            sxx += ph * ph
            syy += cond * cond
            sxy += ph * cond
    except csv.Error as exc:
        raise CsvFormatError(f"CSV 语法错误：{exc}") from exc

    return ReleaseStats(n=n, sx=sx, sy=sy, sxx=sxx, syy=syy, sxy=sxy)


def pearson_from_stats(total: ReleaseStats) -> float | None:
    """由充分统计量计算 Pearson 相关系数。

    样本不足两行或任一列恒定（方差为零）时返回 ``None``。
    输入为精确整数和，同一组输入必然得到逐位相同的结果。
    """
    n = total.n
    if n < 2:
        return None
    num = n * total.sxy - total.sx * total.sy
    den_x = n * total.sxx - total.sx * total.sx
    den_y = n * total.syy - total.sy * total.sy
    # 数学上 den_x/den_y 恒 >= 0（Cauchy-Schwarz），== 0 当且仅当该列恒定。
    if den_x <= 0 or den_y <= 0:
        return None
    r = num / (math.sqrt(den_x) * math.sqrt(den_y))
    # 浮点末位舍入可能使 |r| 略大于 1，钳回定义域。
    return max(-1.0, min(1.0, r))


def total_of(items: dict[int, ReleaseStats]) -> ReleaseStats:
    """把 {发布序号: 统计量} 汇总为总统计量。"""
    total = ReleaseStats()
    for stats in items.values():
        total = total.combine(stats)
    return total
