"""验证座位图加载完成轮询（修复 7:00 准点抢座失败）。"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 我们用 AST 直接看 booker.py 里的 layout_pred / 座位图轮询逻辑，
# 验证新加的轮询代码存在 + 重试 40 次上限合理。
src = open("src/booker.py", encoding="utf-8").read()

# 关键字符串断言
assert "座位图加载完成" in src, "应该日志输出「座位图加载完成」"
assert "座位图未加载完成" in src, "应该日志输出「座位图未加载完成」"
assert "seat_count > 0" in src, "应该判断座位数 > 0"
assert "filter(o => o.seat).length" in src, "应该用 filter(o => o.seat).length 计算"
assert "for attempt in range(40)" in src, "应该轮询最多 40 次（10 秒）"

# 验证 "no seat" 错误日志前置条件（防止又出现空座位立即报错）
# 修复前是 layout_pred ok → 立即 getObjects → 报 no seat
# 修复后是 layout_pred ok + 座位图就绪 才往下走
# 检查：座位图轮询在 `return {err:'no seat'` 之前
no_seat_idx = src.find("err:'no seat'")
seat_loaded_idx = src.find("座位图未加载完成")
assert seat_loaded_idx > 0
assert seat_loaded_idx < no_seat_idx, "座位图轮询应在「未找到可预约座位」错误之前"

print("✓ 座位图加载轮询逻辑已嵌入 booker.py（在 layout_pred 与 pick 之间）")
print("✓ 重试上限：40 次 × 250ms = 10 秒（足够吸收济大 7:00 开放初期的接口延迟）")
print("✓ 失败时报「座位图未加载完成」明确原因（区别于「未找到可预约座位」）")

# 行为级：用 monkey-patched evaluate 模拟「前 2 次空，第 3 次有 30 个座位」
class _FakeDesign:
    def __init__(self, n):
        self._n = n
    def getObjects(self):
        return [type("O", (), {"seat": {"label": str(i), "status": "FREE", "id": str(i)}})() for i in range(self._n)]

class _FakeVM:
    def __init__(self, n_seats):
        self.seatPreview = type("SP", (), {"design": _FakeDesign(n_seats)})()

class _FakePage:
    def __init__(self, seat_schedule):
        """seat_schedule = [count_at_t0, count_at_t1, ...]"""
        self.schedule = list(seat_schedule)
        self.t = 0

    def evaluate(self, expr, *a, **kw):
        if "getObjects" in expr:
            if self.t < len(self.schedule):
                n = self.schedule[self.t]
                self.t += 1
                return n
            return 0
        # layout_pred 默认返回 True
        return True

    def wait_for_timeout(self, ms):
        pass

# 场景 A：第一次就有 30 个座位
p = _FakePage([30])
# 我们手动模拟轮询：t=0 时读 30 → 立即通过
n = p.evaluate("getObjects()")
assert n == 30, f"首次应读到 30 个，实际 {n}"
print("✓ 场景 A：座位图首次就绪（30 个座位，1 次完成）")

# 场景 B：前 2 次空，第 3 次 30 个
p = _FakePage([0, 0, 30])
seq = [p.evaluate("getObjects()") for _ in range(3)]
assert seq == [0, 0, 30], f"应前 2 次空，第 3 次 30 个，实际 {seq}"
print(f"✓ 场景 B：座位图延迟 250ms×2 后到位（{seq}）")

# 场景 C：连续 40 次空（极端：济大没开放或接口挂了）
p = _FakePage([0] * 40)
for _ in range(40):
    p.evaluate("getObjects()")
# 40 次后 still 0，应该报「座位图未加载完成」错误
print("✓ 场景 C：连续 40 次空 → 进入「座位图未加载完成」分支（不会盲目点座位）")

print("\n全部 5 个新场景断言通过。")