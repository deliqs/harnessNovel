import os

_GLOBAL_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".harnessNovel")
_GLOBAL_ENV_PATH = os.path.join(_GLOBAL_CONFIG_DIR, ".env")


def _load_env():
    """Load .env by priority: ~/.harnessNovel/.env, then the current directory."""
    for env_path in [
        _GLOBAL_ENV_PATH,
        os.path.join(os.getcwd(), ".env"),
    ]:
        env = {}
        if not os.path.exists(env_path):
            continue
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip()
        return env
    return {}


class ConfigLoader:
    _env = None

    @classmethod
    def reload(cls):
        """Clear the in-process .env cache so newly saved Web settings are picked up."""
        cls._env = None

    @classmethod
    def activate(cls, updates):
        """Apply runtime config so this process and later children use the new values."""
        for key, value in updates.items():
            os.environ[str(key)] = str(value)
        cls.reload()

    @classmethod
    def _get_env(cls):
        if cls._env is None:
            cls._env = _load_env()
        return cls._env

    @classmethod
    def _build_config(cls, prefix):
        """Build an LLM config dict from environment variables / .env for a prefix."""
        env = cls._get_env()
        return {
            "model": os.getenv(f"{prefix}_MODEL") or env.get(f"{prefix}_MODEL", ""),
            "base_url": os.getenv(f"{prefix}_BASE_URL") or env.get(f"{prefix}_BASE_URL", ""),
            "api_key": os.getenv(f"{prefix}_API_KEY") or env.get(f"{prefix}_API_KEY", ""),
        }

    @classmethod
    def get_data_builder_config(cls):
        """Model config for reference-novel batch extraction (init flow)."""
        return cls._build_config("DATA_BUILDER")

    @classmethod
    def get_adaptive_builder_config(cls):
        """Book-design and stage-design config (pro model recommended)."""
        return cls._build_config("ADAPTIVE_BUILDER")

    @classmethod
    def get_adaptive_builder_lite_config(cls):
        """Story-arc, chapter-outline, draft, and light helper-task config (flash recommended)."""
        return cls._build_config("ADAPTIVE_BUILDER_LITE")
