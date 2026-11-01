"""描述性统计小脚本（仅标准库）。

用法：python quick_stats.py <相对 workspace 的 csv 路径>
"""
from __future__ import annotations

import csv
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # <项目根>/skills/<slug>/scripts/x.py


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python quick_stats.py <csv 路径>")
        return 2

    target = (ROOT / "workspace" / sys.argv[1]).resolve()
    try:
        target.relative_to((ROOT / "workspace").resolve())
    except ValueError:
        print("错误：只能分析 workspace 目录内的文件")
        return 2

    if not target.exists():
        print(f"文件不存在：{sys.argv[1]}")
        return 1

    with open(target, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(fh, dialect=dialect))

    if not rows:
        print("文件里没有数据行")
        return 1

    columns = list(rows[0].keys())
    print(f"文件：{sys.argv[1]}")
    print(f"行数：{len(rows)}    列数：{len(columns)}")
    print(f"列名：{', '.join(columns)}\n")

    for col in columns:
        values = [(r.get(col) or "").strip() for r in rows]
        missing = sum(1 for v in values if v == "")
        non_empty = [v for v in values if v != ""]
        print(f"[{col}] 缺失 {missing}/{len(values)}")

        numbers = []
        for v in non_empty:
            try:
                numbers.append(float(v.replace(",", "")))
            except ValueError:
                numbers = []
                break

        if numbers and len(numbers) == len(non_empty):
            print(
                f"  数值列  最小 {min(numbers):.4g}  最大 {max(numbers):.4g}  "
                f"均值 {statistics.fmean(numbers):.4g}  中位数 {statistics.median(numbers):.4g}"
            )
        elif non_empty:
            top = Counter(non_empty).most_common(5)
            pretty = "，".join(f"{k}×{c}" for k, c in top)
            print(f"  类别列  去重 {len(set(non_empty))} 个  高频：{pretty}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
