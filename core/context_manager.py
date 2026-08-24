import os
import json
import re

class ContextManager:
    def __init__(self, base_dir="file_system"):
        self.base_dir = base_dir
        self.mock_data = None  # Context dict injected from training data.

    def set_mock_data(self, mock_data: dict):
        """
        Inject external mock data into ContextManager (used during training).
        Once set, reads prefer this dict and fall back to the filesystem if missing.
        """
        self.mock_data = mock_data

    def _read_file(self, rel_path, default_key=None):
        if self.mock_data and default_key and default_key in self.mock_data:
            return self.mock_data[default_key]
        
        path = os.path.join(self.base_dir, rel_path)
        if not os.path.exists(path):
            return f"[{rel_path} Not Found]"
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()

    @staticmethod
    def extract_relevant_volume_outline(full_outline, current_chapter, include_next=True):
        """
        Slice and return the volume-outline segments relevant to the current chapter.
        By default include the current segment and the next one (include_next=True)
        so draft generation keeps story continuity. For chapter-outline generation,
        set include_next=False to return only the in-progress segment.
        If parsing fails, return the full volume outline.
        """
        if not full_outline or current_chapter is None:
            return full_outline
            
        header_match = re.search(
            r'^(.*?)(?=\n(?:[一二三四五六七八九十百千万]+、|(?:Act\s+)?[IVX]+\.\s))',
            full_outline,
            re.DOTALL | re.IGNORECASE,
        )
        header = header_match.group(1).strip() + "\n\n" if header_match else ""

        # Chinese: "一、 …（第1章 - 第7章）"; English: "I. ... (Chapters 1 - 7)" / "Act I ..."
        cn_pattern = re.compile(
            r'([一二三四五六七八九十百千万]+、.*?[（\(]第(\d+)章\s*-\s*第(\d+)章[）\)].*?)'
            r'(?=\n[一二三四五六七八九十百千万]+、|$)',
            re.DOTALL,
        )
        en_pattern = re.compile(
            r'((?:Act\s+[IVX]+|[IVX]+\.)\s*.*?[\(（]\s*Chapters?\s+(\d+)\s*[-–—]\s*(\d+)\s*[\)）].*?)'
            r'(?=\n(?:Act\s+[IVX]+|[IVX]+\.)|$)',
            re.DOTALL | re.IGNORECASE,
        )

        segments = []
        for pattern in (cn_pattern, en_pattern):
            for match in pattern.finditer(full_outline):
                segments.append({
                    "start": int(match.group(2)),
                    "end": int(match.group(3)),
                    "text": match.group(1).strip(),
                })
        segments.sort(key=lambda item: (item["start"], item["end"]))
            
        if not segments:
            return full_outline

        relevant_texts = []
        found_idx = -1
        # Find the segment that contains the current chapter.
        for idx, seg in enumerate(segments):
            if seg["start"] <= current_chapter <= seg["end"]:
                relevant_texts.append(seg["text"])
                found_idx = idx
                break
                
        if found_idx == -1:
            # If the chapter is outside every segment, return the first or last.
            if current_chapter < segments[0]["start"]:
                relevant_texts.append(segments[0]["text"])
                found_idx = 0
            else:
                relevant_texts.append(segments[-1]["text"])
                found_idx = len(segments) - 1

        # Optionally append the next segment so story continuity is preserved.
        if include_next and found_idx + 1 < len(segments):
            relevant_texts.append(segments[found_idx + 1]["text"])
            
        return header + "\n\n".join(relevant_texts)

    def get_core_layer(self, system_prompt=None):
        """
        Core layer: system prompt (the novelist's writing style).
        """
        if self.mock_data and "system_prompt" in self.mock_data:
            system_prompt = self.mock_data["system_prompt"]
        elif not system_prompt:
            # If nothing was passed and there is no mock data, try the file.
            system_prompt = self._read_file("system_prompt.md", "system_prompt")
            # If the file is missing, provide a default fallback.
            if "[system_prompt.md Not Found]" in system_prompt:
                system_prompt = (
                    "You are a novelist. Write vivid, specific, continuous prose."
                )
                
        return f"=== Core layer ===\n{system_prompt}\n"

    def get_memory_layer(self):
        """
        Memory layer: novel outline + volume outline + dynamic worldview
        + [prose guide AGENTS.md / chapter-outline guide CHAPTER_AGENTS.md]
        """
        # Compatible with both prose training and chapter-outline training:
        # if the dict has chapter_agents_md, use the chapter-outline guide;
        # otherwise try agents_md, and leave empty if missing.
        rules_text = ""
        if self.mock_data and "chapter_agents_md" in self.mock_data:
            chapter_agents_md = self.mock_data["chapter_agents_md"]
            rules_text = f"--- Chapter-outline writing guide (CHAPTER_AGENTS.md) ---\n{chapter_agents_md}\n\n"
            # Also inject the prose writing guide as a style constraint for outlines.
            if self.mock_data.get("agents_md"):
                rules_text += f"--- Prose writing guide (chapter outlines must follow this style) ---\n{self.mock_data['agents_md']}\n\n"
        else:
            # Compatible with the old flow: if mock_data is missing, or present
            # without stripping agents_md. If mock_data explicitly omits agents_md, skip.
            if self.mock_data is not None and "agents_md" not in self.mock_data:
                pass
            else:
                agents_md = self._read_file("AGENTS.md", "agents_md")
                rules_text = f"--- Core writing guide (AGENTS.md) ---\n{agents_md}\n\n"

        novel_outline = self._read_file("novel_outline.md", "novel_outline")
        volume_outline = self._read_file("volume_outline.md", "volume_outline")
        worldview = self._read_file("dynamic_worldview.md", "dynamic_worldview")

        return (
            f"=== Memory layer ===\n"
            f"{rules_text}"
            f"--- Novel outline ---\n{novel_outline}\n\n"
            f"--- Volume outline ---\n{volume_outline}\n\n"
            f"--- Dynamic worldview ---\n{worldview}\n"
        )

    def get_working_layer(self):
        """
        Working layer: future chapter outlines + character/relation files + foreshadowing pool.
        """
        future_outline = self._read_file("future/chapter_outlines.md", "future_outline")
        characters = self._read_file("characters_and_relations.json", "characters")
        clues = self._read_file("foreshadowing_and_clues.json", "clues")

        return (
            f"=== Working layer ===\n"
            f"--- Future chapter outlines ---\n{future_outline}\n\n"
            f"--- Dynamic character and relation files ---\n{characters}\n\n"
            f"--- Foreshadowing and clue pool ---\n{clues}\n"
        )

    def _read_all_plot_summaries(self):
        """Aggregate plot_summary files across volumes. Compatible with the old single-file format."""
        ps_dir = os.path.join(self.base_dir, "history", "plot_summary")
        if os.path.isdir(ps_dir):
            parts = []
            for f in sorted(os.listdir(ps_dir)):
                if re.match(r'^vol_\d+\.md$', f):
                    with open(os.path.join(ps_dir, f), "r", encoding="utf-8") as fp:
                        content = fp.read().strip()
                        if content:
                            parts.append(content)
            if parts:
                return "\n".join(parts)
        # Compatible with the old single-file format.
        old_path = os.path.join(self.base_dir, "history", "plot_summary.md")
        if os.path.exists(old_path):
            with open(old_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def get_history_layer(self, recent_n=3):
        """
        History layer: prior-plot summary + the last N full chapters (for continuity).
        """
        plot_summary = self._read_all_plot_summaries()
        
        # Load the last N chapters.
        recent_chapters_content = ""
        if self.mock_data and "recent_chapters_content" in self.mock_data:
            recent_chapters_content = self.mock_data["recent_chapters_content"]
        else:
            chapters_dir = os.path.join(self.base_dir, "history/chapters")
            if os.path.exists(chapters_dir):
                # Collect chapter files under all vol_XX subdirectories.
                all_chapters = []
                for entry in sorted(os.listdir(chapters_dir)):
                    entry_path = os.path.join(chapters_dir, entry)
                    if os.path.isdir(entry_path):
                        for ch in sorted(os.listdir(entry_path)):
                            if ch.endswith('.md'):
                                all_chapters.append(os.path.join(entry_path, ch))
                    elif entry.endswith('.md'):
                        all_chapters.append(entry_path)
                recent_chapters = all_chapters[-recent_n:] if len(all_chapters) > recent_n else all_chapters
                for ch_path in recent_chapters:
                    ch_name = os.path.basename(ch_path)
                    with open(ch_path, "r", encoding="utf-8") as f:
                        recent_chapters_content += f"\n--- Chapter: {ch_name} ---\n" + f.read().strip() + "\n"
        
        if not recent_chapters_content:
            recent_chapters_content = "No historical chapter content yet."

        return (
            f"=== History layer ===\n"
            f"--- Prior plot summary ---\n{plot_summary}\n\n"
            f"--- Recent chapters (last {recent_n}) ---\n{recent_chapters_content}\n"
        )

    def build_full_context(self, system_prompt=None):
        """
        Assemble all context layers.
        """
        if system_prompt:
            core = self.get_core_layer(system_prompt)
        else:
            core = self.get_core_layer()
            
        memory = self.get_memory_layer()
        working = self.get_working_layer()
        history = self.get_history_layer()

        return f"{core}\n{memory}\n{working}\n{history}"
