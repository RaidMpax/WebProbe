"""为一次任务提供单调时钟 deadline，避免系统时间调整影响计时。"""

import math
import time


class TimeBudget:
    """记录总时限，并提供可传给模型或浏览器工具的剩余时间。"""

    def __init__(self, max_seconds: float) -> None:
        if not math.isfinite(max_seconds) or max_seconds <= 0:
            raise ValueError("max_seconds 必须是大于 0 的有限秒数")
        self._deadline = time.monotonic() + max_seconds

    def remaining_seconds(self) -> float:
        """返回剩余秒数；预算结束后固定返回 0。"""
        return max(0.0, self._deadline - time.monotonic())

    @property
    def exhausted(self) -> bool:
        return self.remaining_seconds() <= 0
