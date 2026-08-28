from pathlib import Path

from setuptools import setup, find_packages

setup(
    name="harnessNovel",
    version="2.0.2",
    author="Fei Niao one the way",
    description="AI agent for long-form web novel writing",
    long_description=Path("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    url="https://github.com/XTmingyue/harnessNovel",
    license="GPL-3.0",
    packages=find_packages(),
    py_modules=["novel_cli"],
    package_data={
        "core": ["prompts/*/prompt.txt", "system_prompt.md", "agents.md"],
        "webui": ["static/*"],
    },
    entry_points={
        "console_scripts": [
            "novel=novel_cli:main",
        ],
    },
    install_requires=[
        "openai>=1.0.0",
        "charset-normalizer>=3.0",
        "fastapi>=0.110",
        "uvicorn>=0.27",
        "python-multipart>=0.0.9",
    ],
    python_requires=">=3.9",
)
