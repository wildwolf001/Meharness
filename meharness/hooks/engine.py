# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from meharness.hooks.executors import execute_action
from meharness.hooks.models import ActionResult, Hook, HookContext, ToolRejectedError

log = logging.getLogger(__name__)


@dataclass
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool


class HookEngine:
    def __init__(self, hooks: list[Hook] | None = None) -> None:
        self.hooks: list[Hook] = hooks or []
        self._prompt_messages: list[str] = []
        self._notifications: list[HookNotification] = []
        # fire-and-forget 的异步 hook 任务集合：跟踪它们以便 wait_pending_async /
        # 关闭时清理。之前 ensure_future 后不保留引用，子进程会泄漏到下一个
        # 事件循环/测试，Windows 上关闭 loop 时挂死。
        self._async_tasks: set[asyncio.Task] = set()


    def find_matching_hooks(self, event: str, ctx: HookContext) -> list[Hook]:
        matched: list[Hook] = []
        for hook in self.hooks:
            if hook.event != event:
                continue
            if not hook.should_run():
                continue
            if hook.condition is not None and not hook.condition.evaluate(ctx):
                continue
            matched.append(hook)
        return matched


    async def run_hooks(self, event: str, ctx: HookContext) -> None:
        matched = self.find_matching_hooks(event, ctx)
        for hook in matched:
            hook.mark_executed()
            if hook.async_exec:
                task = asyncio.ensure_future(self._run_single(hook, ctx))
                self._async_tasks.add(task)
                task.add_done_callback(self._async_tasks.discard)
            else:
                await self._run_single(hook, ctx)

    async def wait_pending_async(self) -> None:
        """等待所有 fire-and-forget 的异步 hook 完成（测试收尾 / 关闭时调用）。

        否则异步 hook 的子进程会跨测试/跨循环泄漏，Windows 上事件循环关闭时挂死。
        """
        pending = [t for t in self._async_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


    async def _run_single(self, hook: Hook, ctx: HookContext) -> None:
        try:
            result = await execute_action(hook.action, ctx)
            if hook.action.type == "prompt" and result.success:
                self._prompt_messages.append(result.output)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=result.output,
                    success=result.success,
                )
            )
            if not result.success:
                log.warning(
                    "Hook '%s' action failed: %s", hook.id, result.output
                )
        except Exception as e:
            log.warning("Hook '%s' execution error: %s", hook.id, e)
            self._notifications.append(
                HookNotification(
                    hook_id=hook.id,
                    event=hook.event,
                    output=str(e),
                    success=False,
                )
            )


    async def run_pre_tool_hooks(
        self, ctx: HookContext
    ) -> ToolRejectedError | None:
        matched = self.find_matching_hooks("pre_tool_use", ctx)
        for hook in matched:
            hook.mark_executed()
            try:
                result = await execute_action(hook.action, ctx)
                self._notifications.append(
                    HookNotification(
                        hook_id=hook.id,
                        event="pre_tool_use",
                        output=result.output,
                        success=result.success,
                    )
                )
                if hook.reject:
                    return ToolRejectedError(
                        tool=ctx.tool_name,
                        reason=result.output,
                        hook_id=hook.id,
                    )
            except Exception as e:
                log.warning("Hook '%s' execution error: %s", hook.id, e)
        return None

    def get_prompt_messages(self) -> list[str]:
        messages = list(self._prompt_messages)
        self._prompt_messages.clear()
        return messages


    def drain_notifications(self) -> list[HookNotification]:
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications
