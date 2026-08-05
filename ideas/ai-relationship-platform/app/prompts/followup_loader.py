"""Allow-listed package-resource loader for the paid follow-up prompt."""

from importlib.resources import files

from app.prompts.loader import PromptNotFoundError, PromptSet

KNOWN_FOLLOWUP_PROMPT_VERSIONS = frozenset({"followup_v1"})


def load_followup_prompts(version: str) -> PromptSet:
    if version not in KNOWN_FOLLOWUP_PROMPT_VERSIONS or "/" in version or ".." in version:
        raise PromptNotFoundError("Unknown follow-up prompt version")
    root = files("app.prompts").joinpath(version)
    try:
        return PromptSet(
            version,
            *(
                root.joinpath(name).read_text("utf-8")
                for name in ("system.md", "request.md", "repair.md")
            ),
        )
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise PromptNotFoundError("Follow-up prompt resources unavailable") from error
