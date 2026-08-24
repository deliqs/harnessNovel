import os


class PromptLoader:
    _prompts = {}
    _base_dir = os.path.join(os.path.dirname(__file__), "prompts")

    @classmethod
    def load(cls, folder_name: str, **kwargs) -> str:
        """Load prompts/<folder>/prompt.txt, then format."""
        if folder_name not in cls._prompts:
            path = os.path.join(cls._base_dir, folder_name, "prompt.txt")
            if not os.path.exists(path):
                raise FileNotFoundError(f"Prompt template not found: {path}")
            with open(path, "r", encoding="utf-8") as f:
                cls._prompts[folder_name] = f.read()

        template = cls._prompts[folder_name]
        try:
            rendered = template.format(**kwargs)
        except KeyError as e:
            raise KeyError(f"Missing prompt parameter for '{folder_name}': {e}")
        return rendered
