"""The agents page's read-only preview of the probing prompts renders.

It calls the library's prompt builder with stand-ins for the interview-time
context, so it breaks whenever that signature changes -- as it did when the
library started deriving `translation` from the language itself.
"""

import pytest

from ainterviewer.agents.config import ProbingAgentConfig
from app.api.dashboard.projects import preview_probing_prompts


@pytest.mark.anyio
@pytest.mark.parametrize("lang", ["EN", "DA"])
async def test_the_preview_renders(lang):
    preview = await preview_probing_prompts(
        "00000000-0000-4000-8000-000000000000",  # ty: ignore[invalid-argument-type]
        lang,
        ProbingAgentConfig(),
        None,  # ty: ignore[invalid-argument-type]
        None,  # ty: ignore[invalid-argument-type]
    )

    assert preview.system
    assert "«main question" in preview.instruction
    # The language line is always shown: the real one for a project that is
    # not in English, a stand-in where an interview would have none.
    assert ("Danish" in preview.instruction) == (lang == "DA")
    assert ("«interview language" in preview.instruction) == (lang == "EN")
