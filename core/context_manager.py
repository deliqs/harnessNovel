import os
import json
import re

class ContextManager:
    def __init__(self, base_dir="file_system"):
        self.base_dir = base_dir
        self.mock_data = None  # 用于存放从训练数据注入的上下文数据字典

    def set_mock_data(self, mock_data: dict):
        """
        为 ContextManager 注入外部模拟数据（训练时使用）。
        一旦注入，将优先从该字典中获取，若不存在，则依然回退到文件系统读取。
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
        根据当前章节号，截取并返回相关的卷纲段落。
        默认包含当前段落和下一个故事段落 (include_next=True，用于正文生成保证故事连续性)。
        当用于章纲生成时，可设置 include_next=False，使其仅返回当前正在进行的单一故事段落。
        如果无法解析，则返回完整卷纲。
        """
        if not full_outline or current_chapter is None:
            return full_outline
            
        # 提取头部信息（直到第一个类似于 "一、" 的标题出现之前的内容）
        header_match = re.search(r'^(.*?)(?=\n[一二三四五六七八九十百千万]+、)', full_outline, re.DOTALL)
        header = header_match.group(1).strip() + "\n\n" if header_match else ""

        # 匹配每个卷纲段落，例如 "一、 航海初显锋芒，觉醒吸能金手指（第1章 - 第7章）"
        # 注意匹配全角或半角括号，以及可能存在的空格
        pattern = re.compile(r'([一二三四五六七八九十百千万]+、.*?[（\(]第(\d+)章\s*-\s*第(\d+)章[）\)].*?)(?=\n[一二三四五六七八九十百千万]+、|$)', re.DOTALL)
        
        segments = []
        for match in pattern.finditer(full_outline):
            segment_text = match.group(1).strip()
            start_chap = int(match.group(2))
            end_chap = int(match.group(3))
            segments.append({
                "start": start_chap,
                "end": end_chap,
                "text": segment_text
            })
            
        if not segments:
            return full_outline

        relevant_texts = []
        found_idx = -1
        # 找到包含当前章节的段落
        for idx, seg in enumerate(segments):
            if seg["start"] <= current_chapter <= seg["end"]:
                relevant_texts.append(seg["text"])
                found_idx = idx
                break
                
        if found_idx == -1:
            # 如果当前章节超出了所有段落的范围，返回第一个或最后一个
            if current_chapter < segments[0]["start"]:
                relevant_texts.append(segments[0]["text"])
                found_idx = 0
            else:
                relevant_texts.append(segments[-1]["text"])
                found_idx = len(segments) - 1

        # 根据配置决定是否为了保证故事连续性而增加下一个故事段落的卷纲
        if include_next and found_idx + 1 < len(segments):
            relevant_texts.append(segments[found_idx + 1]["text"])
            
        return header + "\n\n".join(relevant_texts)

    def get_core_layer(self, system_prompt=None):
        """
        核心层：System prompt（作家的写作风格）
        """
        if self.mock_data and "system_prompt" in self.mock_data:
            system_prompt = self.mock_data["system_prompt"]
        elif not system_prompt:
            # 如果没有传入，并且没有 mock 数据，则尝试从文件中读取
            system_prompt = self._read_file("system_prompt.md", "system_prompt")
            # 如果文件不存在，则提供一个默认 fallback
            if "[system_prompt.md Not Found]" in system_prompt:
                if os.getenv("HARNESS_NOVEL_LANG", "en").strip().lower() != "zh":
                    system_prompt = (
                        "You are a novelist. Write vivid, specific, continuous prose."
                    )
                else:
                    system_prompt = "你是一个顶级的网文小说家，擅长构建引人入胜的剧情和丰满的人物形象。"
                
        return f"=== 核心层 (Core) ===\n{system_prompt}\n"

    def get_memory_layer(self):
        """
        记忆层：小说大纲 + 卷纲 + 动态世界观 + [正文规范 AGENTS.md / 章纲规范 CHAPTER_AGENTS.md]
        """
        # 兼容正文训练和章纲训练：如果字典里有 chapter_agents_md，就用章纲规范；
        # 否则尝试加载 agents_md，如果没有则为空。
        rules_text = ""
        if self.mock_data and "chapter_agents_md" in self.mock_data:
            chapter_agents_md = self.mock_data["chapter_agents_md"]
            rules_text = f"--- 章纲写作规范 (CHAPTER_AGENTS.md) ---\n{chapter_agents_md}\n\n"
            # 章纲生成时也注入正文写作规范作为风格约束
            if self.mock_data.get("agents_md"):
                rules_text += f"--- 正文写作规范（章纲须遵照此风格规划内容） ---\n{self.mock_data['agents_md']}\n\n"
        else:
            # 兼容老流程，如果没有传入 mock_data 或者有 mock_data 但没有剔除 agents_md
            # 如果 mock_data 里显式把 agents_md 设为 None 或不在字典里，就不加载
            if self.mock_data is not None and "agents_md" not in self.mock_data:
                pass
            else:
                agents_md = self._read_file("AGENTS.md", "agents_md")
                rules_text = f"--- 核心写作规范 (AGENTS.md) ---\n{agents_md}\n\n"

        novel_outline = self._read_file("novel_outline.md", "novel_outline")
        volume_outline = self._read_file("volume_outline.md", "volume_outline")
        worldview = self._read_file("dynamic_worldview.md", "dynamic_worldview")

        return (
            f"=== 记忆层 (Memory) ===\n"
            f"{rules_text}"
            f"--- 小说大纲 ---\n{novel_outline}\n\n"
            f"--- 卷纲 ---\n{volume_outline}\n\n"
            f"--- 动态世界观 ---\n{worldview}\n"
        )

    def get_working_layer(self):
        """
        工作层：未来章节章纲 + 动态人设与关系档案 + 伏笔与线索池 
        """
        future_outline = self._read_file("future/chapter_outlines.md", "future_outline")
        characters = self._read_file("characters_and_relations.json", "characters")
        clues = self._read_file("foreshadowing_and_clues.json", "clues")

        return (
            f"=== 工作层 (Working) ===\n"
            f"--- 未来章节章纲 ---\n{future_outline}\n\n"
            f"--- 动态人设与关系档案 ---\n{characters}\n\n"
            f"--- 伏笔与线索池 ---\n{clues}\n"
        )

    def _read_all_plot_summaries(self):
        """聚合所有卷的 plot_summary 文件。兼容旧的单文件格式。"""
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
        # 兼容旧格式
        old_path = os.path.join(self.base_dir, "history", "plot_summary.md")
        if os.path.exists(old_path):
            with open(old_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def get_history_layer(self, recent_n=3):
        """
        历史层：前文剧情总结（摘要） + 近N章的完整内容（保证延续性）
        """
        plot_summary = self._read_all_plot_summaries()
        
        # 获取近N章内容
        recent_chapters_content = ""
        if self.mock_data and "recent_chapters_content" in self.mock_data:
            recent_chapters_content = self.mock_data["recent_chapters_content"]
        else:
            chapters_dir = os.path.join(self.base_dir, "history/chapters")
            if os.path.exists(chapters_dir):
                # 收集所有 vol_XX 子目录下的章节文件
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
                        recent_chapters_content += f"\n--- 章节: {ch_name} ---\n" + f.read().strip() + "\n"
        
        if not recent_chapters_content:
            recent_chapters_content = "暂无历史章节内容。"

        return (
            f"=== 历史层 (History) ===\n"
            f"--- 前文剧情总结 ---\n{plot_summary}\n\n"
            f"--- 近期章节内容 (近{recent_n}章) ---\n{recent_chapters_content}\n"
        )

    def build_full_context(self, system_prompt=None):
        """
        组装所有上下文
        """
        if system_prompt:
            core = self.get_core_layer(system_prompt)
        else:
            core = self.get_core_layer()
            
        memory = self.get_memory_layer()
        working = self.get_working_layer()
        history = self.get_history_layer()

        return f"{core}\n{memory}\n{working}\n{history}"
