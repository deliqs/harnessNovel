import os


def get_novels_dir():
    """Return the workspace root directory.

    CLI mode keeps the original convention: when unset, use ``my-novels`` under the
    current directory. The Web workbench can point ``HARNESS_NOVEL_HOME`` at a
    user-chosen fixed directory so novels are still found after the server cwd changes.
    """
    configured = os.getenv("HARNESS_NOVEL_HOME")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(os.getcwd(), "my-novels")


# Keep the old constant for external scripts that import it; internals use get_novels_dir().
NOVELS_DIR = get_novels_dir()


class NovelWorkspace:
    """An independent workspace for one novel, with path resolution for every data directory."""

    def __init__(self, name):
        self.name = name
        self.root = os.path.join(get_novels_dir(), name)
        self.file_system = os.path.join(self.root, "file_system")
        self.creative_direction = os.path.join(self.root, "creative_direction.md")
        self.reference = os.path.join(self.root, "reference")
        self.reference_outlines = os.path.join(self.reference, "outlines")
        self.reference_sample = os.path.join(self.reference, "sample_novel.txt")
        self.reference_chapters = os.path.join(self.reference, "chapters")

    # ── directory setup ──

    def ensure_dirs(self):
        """Ensure all required subdirectories exist. Other derived dirs are created on write."""
        for d in [self.root, self.file_system, self.reference, self.reference_outlines, self.reference_chapters]:
            os.makedirs(d, exist_ok=True)


def list_novels():
    """List all existing workspace names."""
    novels_dir = get_novels_dir()
    if not os.path.isdir(novels_dir):
        return []
    return sorted(
        d for d in os.listdir(novels_dir)
        if os.path.isdir(os.path.join(novels_dir, d))
    )


def init_workspace(name):
    """Create or return an existing workspace."""
    ws = NovelWorkspace(name)
    ws.ensure_dirs()
    return ws
