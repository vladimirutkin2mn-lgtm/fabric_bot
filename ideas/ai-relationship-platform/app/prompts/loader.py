"""Allow-listed, package-resource prompt loading."""

from dataclasses import dataclass
from importlib.resources import files


class PromptNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class PromptSet:
    version: str
    system: str
    request: str
    repair: str


KNOWN_PROMPT_VERSIONS = frozenset({"analysis_v1"})


def load_prompts(version: str) -> PromptSet:
    if version not in KNOWN_PROMPT_VERSIONS or "/" in version or ".." in version:
        raise PromptNotFoundError("Unknown prompt version")
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
        raise PromptNotFoundError("Prompt resources unavailable") from error
