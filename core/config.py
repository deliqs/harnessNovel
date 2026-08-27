import os
import tempfile

_GLOBAL_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".harnessNovel")
_GLOBAL_ENV_PATH = os.path.join(_GLOBAL_CONFIG_DIR, ".env")


def write_private_text(path, content):
    """Atomically write private local configuration or diagnostic data on POSIX."""
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".harness-novel-", dir=directory)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


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
    def deactivate(cls, keys):
        """Remove runtime overrides so optional settings resume their file fallback."""
        for key in keys:
            os.environ.pop(str(key), None)
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

    @classmethod
    def get_model_role_config(cls, role):
        """Return an optional production-role config with field-wise Lite fallback.

        ``DRAFT_*``, ``EDITOR_*``, and ``CRITIC_*`` are intentionally optional.
        A partially configured role inherits every missing field from
        ``ADAPTIVE_BUILDER_LITE_*``, preserving existing installations exactly.
        """
        prefix = str(role or "").strip().upper()
        if prefix not in {"DRAFT", "EDITOR", "CRITIC"}:
            raise ValueError("Unknown model role: %s" % role)
        role_config = cls._build_config(prefix)
        lite_config = cls.get_adaptive_builder_lite_config()
        return {
            key: role_config.get(key) or lite_config.get(key, "")
            for key in ("model", "base_url", "api_key")
        }

    @classmethod
    def get_draft_config(cls):
        """Draft-generation config; defaults exactly to the Lite role."""
        return cls.get_model_role_config("DRAFT")

    @classmethod
    def get_editor_config(cls):
        """Editing/humanization config; defaults exactly to the Lite role."""
        return cls.get_model_role_config("EDITOR")

    @classmethod
    def get_critic_config(cls):
        """Critique/validation config; defaults exactly to the Lite role."""
        return cls.get_model_role_config("CRITIC")

    @classmethod
    def get_prompt_trace_mode(cls):
        """Return the supported prompt diagnostic mode, defaulting to metadata only."""
        env = cls._get_env()
        value = (
            os.getenv("HARNESS_NOVEL_PROMPT_TRACE_MODE")
            or env.get("HARNESS_NOVEL_PROMPT_TRACE_MODE", "metadata")
        ).strip().lower()
        if value in {"off", "metadata", "full"}:
            return value
        return "metadata"
