import os

# This fork defaults to English generation. Set HARNESS_NOVEL_LANG=zh for upstream Chinese prompts.
_LANG = os.getenv("HARNESS_NOVEL_LANG", "en").strip().lower() or "en"

_EN_OUTPUT_CONTRACT = """

[Language]
Write the entire response in English. Do not use Chinese except in proper nouns that are already Chinese.
Required section headings must be English (for example: Planned chapters, Stage 1, Story line).
If a template mentions a Chinese-character count, write an English passage of similar density instead.
"""


class PromptLoader:
    _prompts = {}
    _base_dir = os.path.join(os.path.dirname(__file__), "prompts")

    @classmethod
    def load(cls, folder_name: str, **kwargs) -> str:
        """Load prompts/<folder>/prompt.en.txt (English) or prompt.txt, then format."""
        cache_key = f"{folder_name}:{_LANG}"
        if cache_key not in cls._prompts:
            folder = os.path.join(cls._base_dir, folder_name)
            en_path = os.path.join(folder, "prompt.en.txt")
            zh_path = os.path.join(folder, "prompt.txt")
            path = en_path if _LANG != "zh" and os.path.exists(en_path) else zh_path
            if not os.path.exists(path):
                raise FileNotFoundError(f"Prompt template not found: {path}")
            with open(path, "r", encoding="utf-8") as f:
                cls._prompts[cache_key] = f.read()

        template = cls._prompts[cache_key]
        try:
            rendered = template.format(**kwargs)
        except KeyError as e:
            raise KeyError(f"Missing prompt parameter for '{folder_name}': {e}")
        if _LANG != "zh" and not os.path.exists(
                os.path.join(cls._base_dir, folder_name, "prompt.en.txt")):
            rendered = rendered + _EN_OUTPUT_CONTRACT
        return rendered
