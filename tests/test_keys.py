# KeyReader 鼠标（SGR 1006）解析测试。

from __future__ import annotations

from meharness.term.keys import KeyReader, _parse_sgr_mouse


def test_sgr_press_left() -> None:
    ev = _parse_sgr_mouse("<0;10;5", "M")
    assert ev is not None
    assert ev.button == "left"
    assert ev.press and not ev.release
    assert ev.x == 10 and ev.y == 5


def test_sgr_release() -> None:
    ev = _parse_sgr_mouse("<0;10;5", "m")
    assert ev is not None
    assert ev.release and not ev.press


def test_sgr_motion() -> None:
    ev = _parse_sgr_mouse("<32;7;3", "M")
    assert ev is not None
    assert ev.button == "motion"


def test_sgr_wheel() -> None:
    up = _parse_sgr_mouse("<64;1;1", "M")
    assert up is not None and up.button == "wheel_up"
    down = _parse_sgr_mouse("<65;1;1", "M")
    assert down is not None and down.button == "wheel_down"


def test_sgr_modifiers() -> None:
    ev = _parse_sgr_mouse("<16;1;1", "M")  # ctrl
    assert ev is not None and ev.ctrl
    ev = _parse_sgr_mouse("<4;1;1", "M")  # shift
    assert ev is not None and ev.shift
    ev = _parse_sgr_mouse("<8;1;1", "M")  # alt
    assert ev is not None and ev.alt


def test_sgr_invalid_returns_none() -> None:
    assert _parse_sgr_mouse("<abc", "M") is None


def test_read_key_parses_mouse_sequence(monkeypatch) -> None:
    kr = KeyReader()
    kr._msvcrt = False
    # \x1b[<0;10;5M
    seq = [0x1B, 0x5B, 0x3C, 0x30, 0x3B, 0x31, 0x30, 0x3B, 0x35, 0x4D]
    it = iter(seq)
    monkeypatch.setattr(kr, "_read_byte", lambda wait: next(it, -1))
    # 首字节（ESC）走 _read_byte，续字节走 _read_vt_byte
    monkeypatch.setattr(kr, "_read_vt_byte", lambda timeout=0.05: next(it, -1))
    key = kr.read_key()
    assert key[0] == "mouse"
    ev = key[1]
    assert ev.button == "left" and ev.press
    assert ev.x == 10 and ev.y == 5


def test_msvcrt_wide_char_chinese(monkeypatch) -> None:
    """Windows 控制台路径：getwch 返回完整宽字符码点，应直接作为 print 字符。"""
    kr = KeyReader()
    kr._msvcrt = True
    monkeypatch.setattr(kr, "_read_byte", lambda wait: ord("中"))
    assert kr.read_key() == ("print", "中")


def test_msvcrt_partial_sgr_sequence_does_not_hang(monkeypatch) -> None:
    """回归：ConPTY 只送来半截 SGR 序列（ESC + `[<65` 但缺最终字节）时，
    key pump 绝不能永久阻塞（getwch 卡死 → 鼠标/键盘全冻结）。有界读取
    超时后应放弃该序列返回 escape，而不是挂住。"""
    import sys as _sys
    import types

    kr = KeyReader()
    kr._msvcrt = True

    seq = list("\x1b[<65".encode())  # 半截：没有 M
    state = {"pos": 0}

    fake = types.ModuleType("msvcrt")
    fake.kbhit = lambda: state["pos"] < len(seq)
    monkeypatch.setitem(_sys.modules, "msvcrt", fake)

    def read_byte(wait):
        if state["pos"] >= len(seq):
            # 序列耗尽：返回 -1（模拟没有更多输入）
            return -1
        b = seq[state["pos"]]
        state["pos"] += 1
        return b

    monkeypatch.setattr(kr, "_read_byte", read_byte)
    # 半截序列应在有界超时内返回（不阻塞），而不是卡死
    key = kr.read_key()
    assert key == ("escape",)


def test_stdin_vt_path_bounded_read_does_not_hang(monkeypatch) -> None:
    """VT 模式（Windows Terminal 鼠标修复的主路径）：reader 线程队列在无输入/
    半截序列时，_read_vt_byte 必须用有界 get(timeout) 返回，不能阻塞 key pump。"""
    import queue

    kr = KeyReader.__new__(KeyReader)
    kr._msvcrt = False
    kr._vt_enabled = True
    kr._raw = False
    kr._fd = None
    kr._stdin_q = queue.Queue()

    import time

    t0 = time.monotonic()
    r = kr._read_vt_byte(timeout=0.05)
    elapsed = time.monotonic() - t0
    assert r == -1
    assert elapsed < 0.5  # 有界返回，不阻塞


def test_stdin_vt_path_returns_buffered_byte() -> None:
    """VT 模式：队列里已有字节时直接返回；取空后返回 -1。"""
    import queue

    kr = KeyReader.__new__(KeyReader)
    kr._msvcrt = False
    kr._vt_enabled = True
    kr._raw = False
    kr._fd = None
    kr._stdin_q = queue.Queue()
    kr._stdin_q.put(65)  # 'A'
    assert kr._read_vt_byte(timeout=0.05) == 65
    assert kr._read_byte(wait=False) == -1  # 已取空


def test_bracketed_paste_newline_is_literal(monkeypatch) -> None:
    """括号粘贴：\x1b[200~...\x1b[201~ 之间的 \r/\n 是字面换行（进输入框），
    不是 Enter——否则多行粘贴每行都被当成一次提交（claude 的优雅粘贴）。"""
    kr = KeyReader()
    kr._msvcrt = False
    seq = b"\x1b[200~hello\rworld\x1b[201~"
    it = iter(seq)
    monkeypatch.setattr(kr, "_read_byte", lambda wait: next(it, -1))
    monkeypatch.setattr(kr, "_read_vt_byte", lambda timeout=0.05: next(it, -1))
    keys = []
    for _ in range(30):
        k = kr.read_key()
        if k == ("eof",):
            break
        keys.append(k)
    text = "".join(k[1] for k in keys if k[0] == "print")
    assert text == "hello\nworld"  # \r → 字面 \n
    assert ("enter",) not in keys
    assert ("print", "\n") in keys
    assert not kr._pasting  # 粘贴结束后恢复


def test_msvcrt_esc_split_sgr_wheel(monkeypatch) -> None:
    """回归：ConPTY 把 CSI 序列分片到达（ESC 先到、续字节稍后到）时，
    必须有界等待续字节再解析，否则滚轮/方向键丢事件——ESC 被当独立键、
    后续 `[<...M` 泄漏进输入（滚轮不可用的根因之一）。"""
    import sys as _sys
    import time
    import types

    kr = KeyReader()
    kr._msvcrt = True

    seq = list("\x1b[<65;8;12M".encode())
    state = {"pos": 0}

    def fake_kbhit():
        if state["pos"] == 1:
            # 模拟续字节延迟到达：ESC 读出后第一次 kbhit 检查时还没有
            time.sleep(0.005)
        return state["pos"] < len(seq)

    fake = types.ModuleType("msvcrt")
    fake.kbhit = fake_kbhit
    monkeypatch.setitem(_sys.modules, "msvcrt", fake)

    def read_byte(wait):
        b = seq[state["pos"]]
        state["pos"] += 1
        return b

    monkeypatch.setattr(kr, "_read_byte", read_byte)
    key = kr.read_key()
    assert key[0] == "mouse"
    ev = key[1]
    assert ev.button == "wheel_down"
    assert ev.x == 8 and ev.y == 12


def test_msvcrt_ascii(monkeypatch) -> None:
    kr = KeyReader()
    kr._msvcrt = True
    monkeypatch.setattr(kr, "_read_byte", lambda wait: ord("a"))
    assert kr.read_key() == ("print", "a")
