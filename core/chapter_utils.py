import glob
import os
import re


MAX_CHAPTERS_PER_VOLUME = 90

_CN_MAP = {'零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100, '千': 1000}
_CN_DIGITS = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九']

_CH_NUM_RE = re.compile(r'第([一二三四五六七八九十百千零\d]+)章')
_EN_CH_NUM_RE = re.compile(r'(?:Chapter|Ch\.?)\s+(\d+)', re.I)


def chapter_draft_basename(chapter_num, *, raw=False, legacy=False):
    chapter_num = int(chapter_num)
    stem = (
        f"{chapter_num:03d}_第{chapter_num}章"
        if legacy
        else f"{chapter_num:03d}_chapter_{chapter_num}"
    )
    return f"{stem}.raw.md" if raw else f"{stem}.md"


def chapter_draft_write_path(directory, chapter_num, *, raw=False):
    return os.path.join(str(directory), chapter_draft_basename(chapter_num, raw=raw))


def resolve_chapter_draft_path(directory, chapter_num, *, raw=False):
    directory = str(directory)
    english = chapter_draft_write_path(directory, chapter_num, raw=raw)
    if os.path.isfile(english):
        return english
    legacy = os.path.join(directory, chapter_draft_basename(chapter_num, raw=raw, legacy=True))
    if os.path.isfile(legacy):
        return legacy
    return english


def remove_legacy_chapter_draft(directory, chapter_num, *, raw=False):
    directory = str(directory)
    english = os.path.abspath(chapter_draft_write_path(directory, chapter_num, raw=raw))
    legacy = os.path.abspath(os.path.join(
        directory, chapter_draft_basename(chapter_num, raw=raw, legacy=True),
    ))
    if legacy != english and os.path.isfile(legacy):
        os.remove(legacy)


def chapter_draft_delete_paths(refined_dir, raw_dir, chapter_num):
    chapter_num = int(chapter_num)
    refined_dir = str(refined_dir)
    raw_dir = str(raw_dir)
    paths = [
        os.path.join(refined_dir, chapter_draft_basename(chapter_num)),
        os.path.join(refined_dir, chapter_draft_basename(chapter_num, legacy=True)),
        os.path.join(raw_dir, chapter_draft_basename(chapter_num, raw=True)),
        os.path.join(raw_dir, chapter_draft_basename(chapter_num, raw=True, legacy=True)),
    ]
    n = chapter_num
    paths.extend(glob.glob(os.path.join(refined_dir, "versions", f"{n:03d}_chapter_{n}.md_*")))
    paths.extend(glob.glob(os.path.join(refined_dir, "versions", f"{n:03d}_第{n}章.md_*")))
    paths.extend(glob.glob(os.path.join(raw_dir, "versions", f"{n:03d}_chapter_{n}_*.raw.md")))
    paths.extend(glob.glob(os.path.join(raw_dir, "versions", f"{n:03d}_第{n}章_*.raw.md")))
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
        result += _CN_DIGITS[thousands] + '千'
        n %= 1000

    hundreds = n // 100
    if hundreds:
        result += _CN_DIGITS[hundreds] + '百'
        n %= 100
    elif result:
        need_zero = True

    tens = n // 10
    if tens:
        if need_zero:
            result += '零'
            need_zero = False
        if tens == 1 and not result:
            result += '十'
        else:
            result += _CN_DIGITS[tens] + '十'
        n %= 10
    elif result and n > 0:
        need_zero = True

    if n > 0:
        if need_zero:
            result += '零'
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
                    lambda m: f'第{corrected}章',
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
