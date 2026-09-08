"""監控迴圈的輪詢節奏。"""

import asyncio
import time


async def pace(interval: float, started: float) -> float:
    """睡到這一輪的週期結束，並在查詢過久時自動放寬。

    Args:
        interval: 使用者設定的每輪週期秒數。
        started: 這一輪開始時的 time.monotonic()。

    Returns:
        實際採用的週期秒數；查詢比週期還久時會是「查詢耗時 × 2」。
    """
    elapsed = time.monotonic() - started
    effective = max(interval, elapsed * 2)
    await asyncio.sleep(max(0.0, effective - elapsed))
    return effective
