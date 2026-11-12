"""The investigation loop.

    model ──tool calls──> tools ──> model ...
      │                     │
      └─no tool call─> nudge└─submit─> check ──rejected──> model
                                         └──accepted──> END

`tools` numbers every result (R1, R2, ...). `check` rejects a submission whose
evidence cites results that do not exist, or whose proposal the executor would
refuse, and sends the model back with the reasons. The model's tool budget and
cost cap are enforced here; once either is spent, the model can only submit.
"""

import json
import operator
import re
import os
from dataclasses import dataclass
from typing import Annotated, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from agent.prompts import SYSTEM, incident_message
from agent.tools import READ_TOOLS, SubmitArgs, submit
from shared.policy import PolicyError, proposal_problems

MAX_CHECKS = 2
# USD per million tokens, used only if the provider does not report the cost itself.
PRICE_IN = float(os.environ.get("PRICE_IN_PER_M", "0.10"))
PRICE_OUT = float(os.environ.get("PRICE_OUT_PER_M", "0.50"))


@dataclass
class Budget:
    max_tool_calls: int = 25
    max_cost_usd: float = 0.05


class State(TypedDict):
    messages: Annotated[list, add_messages]
    trace: Annotated[list, operator.add]
    results: dict
    tool_calls: int
    cost_usd: float
    tokens: int
    submitted: Optional[dict]
    checks: int
    final: Optional[dict]


def usage(msg):
    meta = getattr(msg, "usage_metadata", None) or {}
    reported = (getattr(msg, "response_metadata", None) or {}).get("token_usage") or {}
    cost = reported.get("cost")
    if cost is None:
        cost = meta.get("input_tokens", 0) * PRICE_IN / 1e6 + meta.get("output_tokens", 0) * PRICE_OUT / 1e6
    return float(cost), int(meta.get("total_tokens", 0))


LEAKED = re.compile(r'<parameter name="(\w+)">(.*?)(?=</parameter>|<parameter name=|$)', re.S)
TRAILING_TAGS = re.compile(r'(\s*</[\w:]+>\s*)+$')


def _value(text):
    text = TRAILING_TAGS.sub("", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        return text


def decode_strings(args):
    """Repair two common ways small models mangle a large tool call:
    nested objects sent as JSON strings, and later arguments leaked into the
    text of an earlier one as <parameter name="..."> markup."""
    out = dict(args)
    for key, value in list(out.items()):
        if isinstance(value, str) and '<parameter name="' in value:
            head, _, tail = value.partition('<parameter name="')
            out[key] = TRAILING_TAGS.sub("", head).strip()
            for name, raw in LEAKED.findall('<parameter name="' + tail):
                out.setdefault(name, _value(raw))
    for key in ("proposal", "evidence", "ruled_out"):
        if isinstance(out.get(key), str):
            out[key] = _value(out[key])
    return out


def evidence_problems(sub, results):
    problems = []
    for i, ev in enumerate(sub.get("evidence") or []):
        refs = ev.get("refs") or []
        missing = [r for r in refs if r not in results]
        failed = [r for r in refs if r in results and not results[r]["ok"]]
        if not refs:
            problems.append(f"evidence[{i}] ('{ev.get('claim', '')[:60]}') cites no result")
        elif missing:
            problems.append(f"evidence[{i}] cites {missing}, which do not exist")
        elif failed and len(failed) == len(refs):
            problems.append(f"evidence[{i}] cites only {failed}, which were refused or failed and show nothing")
    return problems


def submission_problems(sub, results):
    try:
        SubmitArgs.model_validate(sub)
    except ValidationError as e:
        return [str(e)[:1500]]
    problems = evidence_problems(sub, results)
    if sub["action"] in ("propose", "advise") and not sub.get("evidence"):
        problems.append("a diagnosis needs evidence")
    if sub["action"] == "propose":
        problems += proposal_problems(sub.get("proposal"))
    if sub["action"] == "advise" and not sub.get("advice"):
        problems.append("advise needs the exact change in `advice`")
    return problems


def build_graph(llm, budget: Budget):
    by_name = {t.name: t for t in READ_TOOLS}
    with_tools = llm.bind_tools(READ_TOOLS + [submit])
    submit_only = llm.bind_tools([submit], tool_choice="submit")

    def model(state):
        spent = state["tool_calls"] >= budget.max_tool_calls or state["cost_usd"] >= budget.max_cost_usd
        extra = []
        if spent:
            extra = [HumanMessage("Your tool budget is used up. Call submit now with what the evidence "
                                  "shows. If it does not show a cause, escalate.")]
        resp = (submit_only if spent else with_tools).invoke(state["messages"] + extra)
        cost, tokens = usage(resp)
        return {"messages": extra + [resp], "cost_usd": state["cost_usd"] + cost,
                "tokens": state["tokens"] + tokens}

    def tools(state):
        results, n, submitted = dict(state["results"]), state["tool_calls"], state["submitted"]
        messages, trace = [], []
        for call in state["messages"][-1].tool_calls:
            name, args = call["name"], call["args"]
            if name == "submit":
                submitted, content = decode_strings(args), "Received. Checking it."
            elif name in by_name:
                n += 1
                rid = f"R{n}"
                try:
                    text, ok = by_name[name].invoke(args), True
                except PolicyError as e:
                    text, ok = f"refused: {e}", False
                except Exception as e:  # noqa: BLE001 - the model needs to see every failure
                    text, ok = f"error: {type(e).__name__}: {e}"[:1500], False
                results[rid] = {"tool": name, "ok": ok}
                trace.append({"id": rid, "tool": name, "args": args, "ok": ok, "result": text[:800]})
                content = f"[{rid}] {text}"
            else:
                content = f"There is no tool called {name}."
            messages.append(ToolMessage(content=content, tool_call_id=call["id"], name=name))
        return {"messages": messages, "results": results, "tool_calls": n,
                "submitted": submitted, "trace": trace}

    def nudge(state):
        return {"messages": [HumanMessage("Use a tool, or call submit to finish.")]}

    def check(state):
        sub = state["submitted"]
        problems = submission_problems(sub, state["results"])
        if not problems:
            return {"final": sub, "trace": [{"check": "accepted"}]}
        if state["checks"] < MAX_CHECKS:
            note = "Your submission was rejected:\n- " + "\n- ".join(problems) + "\nFix this and call submit again."
            return {"messages": [HumanMessage(note)], "submitted": None, "checks": state["checks"] + 1,
                    "trace": [{"check": "rejected", "problems": problems}]}
        final = {
            "action": "escalate",
            "summary": "The agent could not produce a valid diagnosis. Last attempt: " + str(sub.get("summary", ""))[:500],
            "root_cause": str(sub.get("root_cause", "unknown"))[:500],
            "evidence": [], "ruled_out": [], "check_problems": problems,
        }
        return {"final": final, "trace": [{"check": "gave_up", "problems": problems}]}

    def after_model(state):
        return "tools" if state["messages"][-1].tool_calls else "nudge"

    def after_tools(state):
        return "check" if state["submitted"] else "model"

    def after_check(state):
        return END if state["final"] else "model"

    g = StateGraph(State)
    g.add_node("model", model)
    g.add_node("tools", tools)
    g.add_node("nudge", nudge)
    g.add_node("check", check)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model, ["tools", "nudge"])
    g.add_edge("nudge", "model")
    g.add_conditional_edges("tools", after_tools, ["check", "model"])
    g.add_conditional_edges("check", after_check, [END, "model"])
    return g.compile()


def investigate(llm, incident, feedback, attempt, budget=None, system_prefix="oncall-", max_attempts=3):
    graph = build_graph(llm, budget or Budget())
    state = {
        "messages": [
            SystemMessage(SYSTEM.format(system_prefix=system_prefix, max_attempts=max_attempts)),
            HumanMessage(incident_message(incident, feedback, attempt)),
        ],
        "trace": [], "results": {"A1": {"tool": "alarm", "ok": True}}, "tool_calls": 0, "cost_usd": 0.0, "tokens": 0,
        "submitted": None, "checks": 0, "final": None,
    }
    return graph.invoke(state, {"recursion_limit": 120})
