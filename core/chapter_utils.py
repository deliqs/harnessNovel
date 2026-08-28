import glob
import os
import re


MAX_CHAPTERS_PER_VOLUME = 90

_CN_MAP = {
    "\u96f6": 0, "\u4e00": 1, "\u4e8c": 2, "\u4e09": 3, "\u56db": 4, "\u4e94": 5,
    "\u516d": 6, "\u4e03": 7, "\u516b": 8, "\u4e5d": 9, "\u5341": 10, "\u767e": 100,
    "\u5343": 1000,
}
_CN_DIGITS = [
    "\u96f6", "\u4e00", "\u4e8c", "\u4e09", "\u56db",
    "\u4e94", "\u516d", "\u4e03", "\u516b", "\u4e5d",
]
_CN_NUM_CLASS = "".join(_CN_MAP)
_CH_NUM_RE = re.compile("\u7b2c([%s\\d]+)\u7ae0" % _CN_NUM_CLASS)
_EN_CH_NUM_RE = re.compile(r'(?:Chapter|Ch\.?)\s+(\d+)', re.I)


def chapter_draft_basename(chapter_num, *, raw=False, legacy=False):
    chapter_num = int(chapter_num)
    _ = legacy
    stem = f"{chapter_num:03d}_chapter_{chapter_num}"
    return f"{stem}.raw.md" if raw else f"{stem}.md"


def chapter_draft_write_path(directory, chapter_num, *, raw=False):
    return os.path.join(str(directory), chapter_draft_basename(chapter_num, raw=raw))


def resolve_chapter_draft_path(directory, chapter_num, *, raw=False):
    return chapter_draft_write_path(directory, chapter_num, raw=raw)


def remove_legacy_chapter_draft(directory, chapter_num, *, raw=False):
    return


def chapter_draft_delete_paths(refined_dir, raw_dir, chapter_num):
    chapter_num = int(chapter_num)
    refined_dir = str(refined_dir)
    raw_dir = str(raw_dir)
    n = chapter_num
    paths = [
        os.path.join(refined_dir, chapter_draft_basename(chapter_num)),
        os.path.join(raw_dir, chapter_draft_basename(chapter_num, raw=True)),
    ]
    paths.extend(glob.glob(os.path.join(refined_dir, "versions", f"{n:03d}_chapter_{n}.md_*")))
    paths.extend(glob.glob(os.path.join(raw_dir, "versions", f"{n:03d}_chapter_{n}_*.raw.md")))
    return paths


def _cn_to_int(s):
    """Convert a Chinese numeral to an integer."""
    if s.isdigit():
        return int(s)
    result = 0
    temp = 0
    for ch in s:
        if ch not in _CN_MAP:
            continue
        v = _CN_MAP[ch]
        if v >= 10:
            if temp == 0:
                temp = 1
            result += temp * v
            temp = 0
        else:
            temp = v
    result += temp
    return result


def _int_to_cn(n):
    """Convert an integer (0-9999) to a Chinese numeral."""
    if n < 10:
        return _CN_DIGITS[n]

    result = ''
    need_zero = False

    thousands = n // 1000
    if thousands:
        result += _CN_DIGITS[thousands] + "\u5343"
        n %= 1000

    hundreds = n // 100
    if hundreds:
        result += _CN_DIGITS[hundreds] + "\u767e"
        n %= 100
    elif result:
        need_zero = True

    tens = n // 10
    if tens:
        if need_zero:
            result += "\u96f6"
            need_zero = False
        if tens == 1 and not result:
            result += "\u5341"
        else:
            result += _CN_DIGITS[tens] + "\u5341"
        n %= 10
    elif result and n > 0:
        need_zero = True

    if n > 0:
        if need_zero:
            result += "\u96f6"
        result += _CN_DIGITS[n]

    return result


def _extract_novel_chapter_num(title):
    """Extract the novel chapter number from a chapter title. Return int, or None on failure."""
    m = _CH_NUM_RE.search(title or "")
    if m:
        return _cn_to_int(m.group(1))
    m = _EN_CH_NUM_RE.search(title or "")
    if m:
        return int(m.group(1))
    return None


def _fix_chapter_numbering(groups):
    """Fix per-volume chapter numbers when they drift >= 50 from the previous number + 1."""
    fixed_total = 0
    for vi, g in enumerate(groups):
        vol_chapters = g["chapters"]
        if not vol_chapters:
            continue

        last_valid = None
        fixed = 0

        for ci, ch in enumerate(vol_chapters):
            nn = _extract_novel_chapter_num(ch["title"])
            if nn is None:
                continue

            if last_valid is None:
                last_valid = nn
                continue

            expected = last_valid + 1
            if abs(nn - expected) >= 50:
                corrected = _int_to_cn(expected)
                old_title = ch["title"]
                new_title = _CH_NUM_RE.sub(
                    lambda m: "\u7b2c%s\u7ae0" % corrected,
                    old_title, count=1,
                )
                ch["title"] = new_title
                fixed += 1
                fixed_total += 1
                print(f"    Fixed volume {vi+1} ci={ci}: {old_title[:25]:25s} → {new_title[:25]}")
                last_valid = expected
            else:
                last_valid = nn

        if fixed:
            print(f"  Volume {vi+1} fixed {fixed} chapter numbers")

    if fixed_total:
        print(f"  Fixed {fixed_total} chapter numbers in total")
    return fixed_total
