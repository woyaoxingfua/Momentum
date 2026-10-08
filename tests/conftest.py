"""共享的测试隔离设施。

有些用例会真实启动 HTTP 服务或调用 cli.main()，它们会往 root logger 挂 StreamHandler，
而 pytest 会在用例结束后关闭捕获流；于是后续异步日志就会往已关闭的流写，
刷出 “ValueError: I/O operation on closed file” 的假故障。
这里在每个用例前后清理这类失效 handler，保证输出干净、不掩盖真实错误。
"""
from __future__ import annotations

import logging

import pytest


def _drop_handlers_writing_to_closed_streams() -> list[logging.Handler]:
    root = logging.getLogger()
    removed: list[logging.Handler] = []
    for handler in list(root.handlers):
        stream = getattr(handler, "stream", None)
        if stream is not None and getattr(stream, "closed", False):
            root.removeHandler(handler)
            removed.append(handler)
    return removed


@pytest.fixture(autouse=True)
def _clean_logging_handlers():
    # 后台线程可能在用例结束后才写日志，此时捕获流已关闭；
    # logging 默认会把这种 handler 异常打到 stderr，看起来像真故障，这里按官方开关静音。
    logging.raiseExceptions = False
    _drop_handlers_writing_to_closed_streams()
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        _drop_handlers_writing_to_closed_streams()
        logging.raiseExceptions = False

