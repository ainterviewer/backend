"""What keeps a project out of distributed interviews: a model or speech
service configured for tests only.

A deployment lists the models respondents' answers may be sent to in `[llm]
models` and the rest in `test_only_models` (a model in neither counts as test
only), and marks speech with `[services.speech] test_only`. Drafts and test
interviews may use anything; publishing refuses a draft that uses a test-only
one (`PublishError`), and a distributed interview is refused on a published
version that does (`NotDistributableError`), which covers versions published
before the rule. `with_audio` is in the versioned interview config
(`ContentColumns.config`), so it is checked like the agents.
"""

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.config import InterviewConfig
from ainterviewer.settings import settings as lib_settings

from .settings import app_settings

#: The key speech is reported under, beside the agents' names.
SPEECH = "speech"

#: Agents that run only in synthetic tests, so never on respondents' answers.
_TEST_AGENTS = frozenset({"answering"})
#: Agents that run only when included; every other agent always runs.
_OPTIONAL_AGENTS = frozenset({"security", "visual"})


def runs_on_answers(agent: str, include: bool) -> bool:
    """Whether `agent` (an `AgentConfigs` field) runs on respondents' answers
    in a distributed interview, given its `include`."""
    if agent in _TEST_AGENTS:
        return False
    return include or agent not in _OPTIONAL_AGENTS


def speech_is_test_only() -> bool:
    return app_settings.services.speech.test_only


def uses_of_test_only(
    agent_configs: AgentConfigs, config: InterviewConfig
) -> dict[str, str]:
    """What a project would send respondents' answers to that is for tests
    only: each agent's name with its model, and `SPEECH` with the speech
    models while `with_audio` is on. Empty when it may be distributed."""
    uses: dict[str, str] = {}
    for name in AgentConfigs.model_fields:
        agent = getattr(agent_configs, name)
        if not runs_on_answers(name, agent.include):
            continue
        if lib_settings.llm.is_test_only(agent.model):
            uses[name] = agent.model
    if config.with_audio and speech_is_test_only():
        speech = app_settings.services.speech
        models = [m for m in (speech.stt_model, speech.tts_model) if m]
        uses[SPEECH] = ", ".join(models) or SPEECH
    return uses


def describe_uses(uses: dict[str, str]) -> str:
    """The agents and services in `uses`, for a message: "the probing and
    security agents and speech"."""
    agents = [name for name in uses if name != SPEECH]
    parts = []
    if agents:
        names = (
            agents[0]
            if len(agents) == 1
            else ", ".join(agents[:-1]) + " and " + agents[-1]
        )
        parts.append(f"the {names} agent{'s' if len(agents) > 1 else ''}")
    if SPEECH in uses:
        parts.append("speech")
    return " and ".join(parts)


def switch_test_only(agent_configs: AgentConfigs, to: str) -> AgentConfigs:
    """A copy of `agent_configs` with every agent `uses_of_test_only` would
    report moved to `to`. Speech is not a model and is left as it is."""
    switched = agent_configs.model_copy(deep=True)
    for name in AgentConfigs.model_fields:
        agent = getattr(switched, name)
        if runs_on_answers(name, agent.include) and lib_settings.llm.is_test_only(
            agent.model
        ):
            agent.model = to
    return switched
