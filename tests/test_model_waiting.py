"""A respondent whose model cannot answer yet is told at once, and waited
for on the open socket; one whose model is not coming is told that instead.

`run_with_waits` is the interview websocket's loop, driven here with a fake
interview run, a fake clock and a list for the socket: what is tested is
what the respondent is sent, and when the turn is tried again.
"""

import asyncio
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from any_llm.exceptions import AuthenticationError, ProviderError
from fastapi import WebSocketDisconnect

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.exceptions import ModelUnavailable, UnavailableReason
from app.api.websockets.interviews import ai
from app.api.websockets.interviews.ai import (
    EXPLAINED_WAIT,
    UNEXPLAINED_RETRY_AFTER,
    UNEXPLAINED_WAIT,
    Wait,
    run_with_waits,
    wait_for,
)
from app.services import inference_warmup
from app.settings import app_settings

SELF_HOSTED = "openai/gpt-oss-120b"


def starting(retry_after: int = 90) -> ModelUnavailable:
    return ModelUnavailable("m", UnavailableReason.STARTING, retry_after=retry_after)


DOWN = ModelUnavailable("m", UnavailableReason.DOWN, retry_after=None)


class Clock:
    """`ai._now` and `ai._pause`: pausing moves time on, and nothing sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []

    def time(self) -> float:
        return self.now

    async def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(ai, "_now", clock.time)
    monkeypatch.setattr(ai, "_pause", clock.pause)
    return clock


class Interview:
    """Runs that fail with `failures` in turn, then go to the end. Each run
    may store a message first (`progress`), as a turn that gets further does.
    """

    def __init__(self, failures, progress: bool = False) -> None:
        self.failures = list(failures)
        self.progress = progress
        self.messages: list[str] = []
        self.runs: list[int] = []
        self.sent: list[dict] = []

    async def run(self, stored) -> None:
        self.runs.append(len(stored.messages))
        if self.progress:
            self.messages.append("answer")
        if self.failures:
            raise self.failures.pop(0)

    def reload(self):
        return SimpleNamespace(messages=list(self.messages))

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)

    def go(self) -> bool:
        return asyncio.run(
            run_with_waits(
                self.reload(), self.run, self.reload, self.send, interview_id="i"
            )
        )


# --- what is worth waiting for ---


@pytest.mark.parametrize(
    ("error", "wait"),
    [
        (starting(87), Wait("starting", 87, EXPLAINED_WAIT)),
        (
            ModelUnavailable("m", UnavailableReason.BUSY, retry_after=5),
            Wait("busy", 5, EXPLAINED_WAIT),
        ),
        # The proxy says nothing is coming: no wait at all.
        (DOWN, None),
        # Nobody explained it: a short wait, on a short budget.
        (
            ProviderError("500"),
            Wait("busy", UNEXPLAINED_RETRY_AFTER, UNEXPLAINED_WAIT),
        ),
        (AuthenticationError("bad key"), None),
        # Our own bug: no better on the second try.
        (RuntimeError("bug"), None),
    ],
)
def test_wait_for(error, wait):
    assert wait_for(error) == wait


# --- the loop ---


def test_an_interview_that_runs_sends_nothing_extra(clock):
    interview = Interview([])

    assert interview.go() is True
    assert interview.sent == []
    assert clock.pauses == []


def test_a_cold_start_is_told_at_once_and_waited_for(clock):
    interview = Interview([starting(90), starting(10)])

    assert interview.go() is True
    assert interview.sent == [
        {"type": "waiting", "reason": "starting", "retry_after": 90},
        {"type": "waiting", "reason": "starting", "retry_after": 10},
    ]
    assert clock.pauses == [90, 10]
    assert len(interview.runs) == 3


def test_a_model_that_is_not_coming_is_reported_at_once(clock):
    interview = Interview([DOWN])

    assert interview.go() is False
    assert interview.sent == [
        {"type": "data", "content": None, "progress": None, "error": "InferenceError"}
    ]
    assert clock.pauses == []


def test_waiting_stops_at_its_budget(clock):
    """Five minutes of a model that never comes, then the respondent is told."""
    interview = Interview([starting(100)] * 10)

    assert interview.go() is False
    # Waited at 0, 100 and 200; at 300 another 100 would pass the budget.
    assert clock.pauses == [100, 100, 100]
    assert interview.sent[-1]["error"] == "InferenceError"
    assert [frame["type"] for frame in interview.sent] == ["waiting"] * 3 + ["data"]


def test_an_unexplained_failure_gets_a_short_budget(clock):
    interview = Interview([ProviderError("502")] * 10)

    assert interview.go() is False
    assert sum(clock.pauses) <= UNEXPLAINED_WAIT
    assert interview.sent[-1]["error"] == "InferenceError"


def test_a_turn_that_gets_further_starts_a_new_budget(clock):
    """Each failure comes after a turn that stored something: a respondent
    who waited through one cold start is not charged for it at the next."""
    failures = [ProviderError("502")] * 10
    interview = Interview(failures, progress=True)

    assert interview.go() is True
    assert sum(clock.pauses) > UNEXPLAINED_WAIT


def test_the_respondent_leaving_is_not_a_failure(clock):
    interview = Interview([WebSocketDisconnect()])

    with pytest.raises(WebSocketDisconnect):
        interview.go()
    assert interview.sent == []


def test_a_closed_socket_stops_the_wait(clock):
    interview = Interview([starting(30), starting(30)])

    async def closed(frame):
        raise RuntimeError("websocket closed")

    interview.send = closed

    assert interview.go() is False
    assert clock.pauses == []
    assert len(interview.runs) == 1


# --- warming up ---


def test_an_interview_on_hosted_models_needs_no_warm_up():
    assert not inference_warmup.needs_self_hosted(AgentConfigs())


def test_one_agent_on_the_self_hosted_model_needs_it():
    configs = AgentConfigs.model_validate({"probing": {"model": SELF_HOSTED}})
    assert inference_warmup.needs_self_hosted(configs)


def test_agents_that_do_not_run_on_answers_do_not_count():
    """The synthetic interviewee, and a security agent left out."""
    configs = AgentConfigs.model_validate(
        {
            "answering": {"model": SELF_HOSTED},
            "security": {"model": SELF_HOSTED, "include": False},
        }
    )
    assert not inference_warmup.needs_self_hosted(configs)


@pytest.fixture
def warm_ups(monkeypatch) -> list[None]:
    """Warm-up switched on, its request recorded instead of sent."""
    monkeypatch.setattr(app_settings.services.inference, "warm_up", True)
    monkeypatch.setattr(inference_warmup, "_last_warm_up", None)
    asked: list[None] = []

    async def ask() -> None:
        asked.append(None)

    monkeypatch.setattr(inference_warmup, "ask_availability", ask)
    return asked


SELF_HOSTED_CONFIGS = AgentConfigs.model_validate({"probing": {"model": SELF_HOSTED}})


def test_warm_up_asks_once_per_interval(warm_ups):
    async def open_three():
        for _ in range(3):
            inference_warmup.warm_up(SELF_HOSTED_CONFIGS)
        await asyncio.gather(*inference_warmup._pending)

    asyncio.run(open_three())
    assert len(warm_ups) == 1


@pytest.mark.parametrize("configs", [None, AgentConfigs()])
def test_warm_up_skips_what_needs_none(warm_ups, configs):
    async def open_one():
        inference_warmup.warm_up(configs)

    asyncio.run(open_one())
    assert warm_ups == []


def test_warm_up_can_be_switched_off(warm_ups, monkeypatch):
    monkeypatch.setattr(app_settings.services.inference, "warm_up", False)

    async def open_one():
        inference_warmup.warm_up(SELF_HOSTED_CONFIGS)

    asyncio.run(open_one())
    assert warm_ups == []


@pytest.mark.parametrize(
    "respond",
    [
        lambda request: httpx.Response(200, json={"ready": True}),
        lambda request: httpx.Response(
            200,
            json={"ready": False, "code": "capacity_starting", "retry_after": 90},
        ),
        # Not our proxy.
        lambda request: httpx.Response(404),
        lambda request: httpx.Response(500, text="boom"),
    ],
)
def test_asking_for_availability_never_raises(monkeypatch, respond):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    monkeypatch.setattr(
        inference_warmup.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )

    asyncio.run(inference_warmup.ask_availability())

    assert [r.method for r in requests] == ["POST"]
    assert requests[0].url.path.endswith("/v1/availability")


def test_an_unreachable_proxy_is_logged_not_raised(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        inference_warmup.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )

    asyncio.run(inference_warmup.ask_availability())
