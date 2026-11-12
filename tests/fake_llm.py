"""A chat model that replays scripted responses, for testing the graph without a provider."""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedLLM(BaseChatModel):
    script: list
    calls: list = []
    bound: list = []

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, tool_choice=None, **kwargs):
        self.bound.append((sorted(t.name for t in tools), tool_choice))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        step = self.script.pop(0)
        if isinstance(step, str):
            msg = AIMessage(content=step)
        else:
            msg = AIMessage(content="", tool_calls=[
                {"name": n, "args": a, "id": f"c{len(self.calls)}-{i}"} for i, (n, a) in enumerate(step)])
        msg.usage_metadata = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
        return ChatResult(generations=[ChatGeneration(message=msg)])


def read(service, operation, **params):
    return ("aws_read", {"service": service, "operation": operation, "params": params})


def submit(**kw):
    return ("submit", kw)
