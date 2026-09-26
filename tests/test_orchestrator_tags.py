"""The UI's message tags are read server-side from the latest author prompt, never from the model."""
import unittest

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from tests.scoped_tool_case import ScopedToolCase
from webui.orchestrator.tools.draft import draft_generate
from webui.orchestrator.tools.tags import (
    AuthorTags,
    latest_author_prompt,
    parse_tags,
    strip_tag_header,
)

ID_A = "0123456789abcdef0123456789abcdef"
ID_B = "fedcba9876543210fedcba9876543210"


def prompt(text):
    return ModelRequest(parts=[UserPromptPart(text)])


def reply(text="ok"):
    return ModelResponse(parts=[TextPart(text)])


class ParseTagsTests(unittest.TestCase):
    def test_reads_every_tag_from_the_header_line(self):
        text = f"[use_new_reference] [attached upload {ID_A}: notes.md] [attached upload {ID_B}: a b.txt]\nGo."
        self.assertEqual(parse_tags(text), AuthorTags(upload_ids=(ID_A, ID_B), use_new_reference=True))

    def test_reads_sync_and_humanize_flags(self):
        self.assertEqual(parse_tags("[sync_updated_design]\nx"), AuthorTags(sync_updated_design=True))
        self.assertEqual(parse_tags("[humanize: off]\nWrite it."), AuthorTags(humanize_off=True))

    def test_a_header_without_text_still_counts(self):
        self.assertEqual(parse_tags(f"[attached upload {ID_A}: n.md]\n").upload_ids, (ID_A,))

    def test_tags_outside_the_header_line_are_ignored(self):
        self.assertEqual(parse_tags("Please sync.\n[use_new_reference]"), AuthorTags())
        self.assertEqual(parse_tags("Write [humanize: off] later"), AuthorTags())
        self.assertEqual(parse_tags(""), AuthorTags())

    def test_malformed_upload_ids_are_ignored_and_duplicates_dropped(self):
        text = f"[attached upload 1234: x.md] [attached upload {ID_A}: a] [attached upload {ID_A}: a]\nGo"
        self.assertEqual(parse_tags(text).upload_ids, (ID_A,))

    def test_strip_tag_header_keeps_untagged_text(self):
        self.assertEqual(strip_tag_header(f"[attached upload {ID_A}: n.md]\nGo."), "Go.")
        self.assertEqual(strip_tag_header("[Note] keep this\nand this"), "[Note] keep this\nand this")


class LatestAuthorPromptTests(unittest.TestCase):
    def test_only_the_latest_prompt_counts(self):
        messages = [prompt("[use_new_reference]\nSync."), reply(), prompt("Change the hero.")]
        self.assertEqual(latest_author_prompt(messages), "Change the hero.")

    def test_auto_turns_are_skipped(self):
        messages = [prompt("[humanize: off]\nWrite."), reply(), prompt("[auto] The job finished.")]
        self.assertEqual(latest_author_prompt(messages), "[humanize: off]\nWrite.")

    def test_list_content_and_empty_history(self):
        self.assertEqual(latest_author_prompt([ModelRequest(parts=[UserPromptPart(["a", "b"])])]), "a\nb")
        self.assertEqual(latest_author_prompt([]), "")


class DraftHumanizeTagTests(ScopedToolCase):
    phase = "draft"

    def test_the_humanize_off_tag_overrides_the_model(self):
        self.write_arc(1, 1, 1, 10)
        ctx = self.ctx({"volume": 1, "arc": 1})
        ctx.messages = [prompt("[humanize: off]\nWrite the arc.")]

        draft_generate(ctx, "Write the arc.", humanize=True)

        self.runtime.draft_chat.start_message.assert_called_once_with("book", 1, 1, "Write the arc.", humanize=False)

    def test_a_copied_tag_line_is_stripped_from_the_draft_message(self):
        self.write_arc(1, 1, 1, 10)
        ctx = self.ctx({"volume": 1, "arc": 1})
        ctx.messages = [prompt("[humanize: off]\nWrite the arc.")]

        draft_generate(ctx, "[humanize: off]\nWrite the arc.")

        self.runtime.draft_chat.start_message.assert_called_once_with("book", 1, 1, "Write the arc.", humanize=False)

    def test_an_old_humanize_tag_does_not_reapply(self):
        self.write_arc(1, 1, 1, 10)
        ctx = self.ctx({"volume": 1, "arc": 1})
        ctx.messages = [prompt("[humanize: off]\nWrite."), reply(), prompt("Revise chapter 2.")]

        draft_generate(ctx, "Revise chapter 2.")

        self.assertTrue(self.runtime.draft_chat.start_message.call_args.kwargs["humanize"])


if __name__ == "__main__":
    unittest.main()
