# 行流式引擎 Phase 1 demo：肉眼验证"提交滚动区 + 输入行重绘"
# 用法：python term_demo.py
# 预期：内容按顺序出现并留在原生 scrollback；输入行始终在底部；
#       退出后干净收尾。

import sys
import time

sys.path.insert(0, ".")

from meharness.term.ansi import setup_utf8
from meharness.term.stream import LineStream


def main() -> None:
    setup_utf8()
    ls = LineStream()
    ls.start()

    # 1. banner + 用户消息
    ls.commit_text("Meharness 行流式 demo")
    ls.commit_text("❯ 你好，请演示行流式渲染")

    # 2. 模拟流式：响应在活跃块内增长（原地重绘，超出屏幕进 scrollback）
    for i in range(1, 9):
        ls.set_response([f"● 流式行 {j} …" for j in range(1, i + 1)])
        time.sleep(0.15)

    # 3. 提交响应
    ls.commit()

    # 4. 系统消息
    ls.commit_text("  ↻ 系统消息：已复制 12 字符")

    # 5. 输入行 + 光标
    text = "正在输入"
    ls.set_input(text, cursor=len(text))
    time.sleep(0.5)

    # 6. 清理退出
    ls.restore()
    print("")


if __name__ == "__main__":
    main()
