"""日本語組版エンジン。

訳文（テキストと保護要素の列）を、1 つ以上の矩形領域へ流し込む。

- 組版単位（Atom）: 和文 1 字 / 欧文の塊（空白で区切られた単語） / 空白 / 保護要素（数式など）
- 禁則処理: 行頭禁則文字は前の行に追い込み、収まらなければ直前の 1 字ごと次行へ追い出す
- 和欧間: 和文と欧文・数式の間に 1/4 em を自動で空ける
- 両端揃え: 段落の最終行以外は行幅いっぱいに均等配置
- 縮小: 収まる最大の倍率を二分探索で求める。領域からはみ出すことはない
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum

from .fontbook import FontBook, FontRole
from .markup import Token
from .models import Align, InlineRun, Rect, TextStyle

# 行頭禁則（行の先頭に来てはいけない）
NO_START = set("、。，．・：；？！゛゜ヽヾゝゞ々ー）］｝」』】〕〉》〙〗’”ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ…‥%),.:;!?]}")
# 行末禁則（行の末尾に来てはいけない）
NO_END = set("（［｛「『【〔〈《〘〖‘“([{")
# 和欧間スペースを入れない和文側の約物
_CJK_PUNCT = set("、。，．・：；？！（）［］｛｝「」『』【】〔〕〈〉《》〘〙〖〗‘’“”…‥")

_CJK_RE = re.compile(
    r"[　-〿぀-ゟ゠-ヿㇰ-ㇿ㐀-䶿一-鿿"
    r"豈-﫿＀-￯]"
)

AUTOSPACE_EM = 0.25
ASCENT = 0.88  # 和文グリフの上端（em 比）
DESCENT = 0.12
EPS = 0.01


class AtomKind(Enum):
    CJK = 1
    LATIN = 2
    SPACE = 3
    INLINE = 4


@dataclass
class Glyphs:
    """同一フォント・サイズ比・持ち上げ量で描く文字列。"""

    text: str
    role: FontRole
    size_ratio: float = 1.0
    rise_ratio: float = 0.0
    color: int | None = None  # None なら段落の色
    bold: bool = False
    unit_width: float = 0.0  # size=1 のときの幅
    dx: float | None = None  # 単位の左端からの位置（em）。None なら直前の続きに置く


@dataclass
class Atom:
    kind: AtomKind
    glyphs: list[Glyphs]
    unit_width: float  # size=1 のときの幅
    no_break_before: bool = False
    no_break_after: bool = False
    cjk_punct: bool = False
    run_id: int | None = None
    scale: float = 1.0  # 保護要素を行に収めるための縮小率


@dataclass
class PlacedGlyphs:
    page: int
    x: float
    y: float  # ベースライン
    text: str
    role: FontRole
    size: float
    color: int
    bold: bool
    run_id: int | None = None  # 保護要素（数式など）のグリフなら、その番号


@dataclass
class PlacedRun:
    """保護要素の配置結果。原文の位置からの移動量の計算に使う。"""

    run_id: int
    page: int
    x: float  # 左端
    baseline: float
    factor: float  # 原文に対する拡大率


@dataclass
class Frame:
    """流し込み先の領域。"""

    page: int
    rect: Rect
    style: TextStyle
    first_baseline_offset: float | None = None  # 原文の 1 行目ベースラインの rect 上端からの距離


@dataclass
class LayoutResult:
    placed: list[PlacedGlyphs]
    scale: float  # 原文サイズに対する実際の倍率
    fits: bool
    line_count: int
    used: list[Rect] = field(default_factory=list)  # 領域ごとの使用範囲
    runs: list[PlacedRun] = field(default_factory=list)


# ---- 組版単位への分解 --------------------------------------------------------


def is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def build_atoms(tokens: list[Token], runs: dict[int, InlineRun], fonts: FontBook, bold: bool) -> list[Atom]:
    atoms: list[Atom] = []
    text_role = FontRole.LATIN_BOLD if bold else FontRole.LATIN
    for tok in tokens:
        if tok.run_id is not None:
            run = runs.get(tok.run_id)
            if run is not None and run.text.strip():
                atom = _inline_atom(run, fonts)
                atom.run_id = tok.run_id
                atoms.append(atom)
            continue
        _text_atoms(tok.text, fonts, text_role, bold, atoms)
    # 先頭・末尾・連続する空白を整理
    cleaned: list[Atom] = []
    for a in atoms:
        if a.kind == AtomKind.SPACE and (not cleaned or cleaned[-1].kind == AtomKind.SPACE):
            continue
        cleaned.append(a)
    while cleaned and cleaned[-1].kind == AtomKind.SPACE:
        cleaned.pop()
    return cleaned


def _text_atoms(text: str, fonts: FontBook, latin_role: FontRole, bold: bool, out: list[Atom]) -> None:
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace() and ch != "　":
            j = i
            while j < n and text[j].isspace() and text[j] != "　":
                j += 1
            w = fonts.width(" ", latin_role, 1.0)
            out.append(Atom(AtomKind.SPACE, [Glyphs(" ", latin_role, unit_width=w)], w))
            i = j
            continue
        if is_cjk(ch):
            role = fonts.resolve(ch, fonts.jp_role(bold))
            w = fonts.width(ch, role, 1.0)
            out.append(
                Atom(
                    AtomKind.CJK,
                    [Glyphs(ch, role, bold=bold and role == FontRole.JP, unit_width=w)],
                    w,
                    no_break_before=ch in NO_START,
                    no_break_after=ch in NO_END,
                    cjk_punct=ch in _CJK_PUNCT,
                )
            )
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace() and not is_cjk(text[j]):
            j += 1
        word = text[i:j]
        glyphs = []
        for part, role in fonts.split_by_coverage(word, latin_role):
            glyphs.append(Glyphs(part, role, bold=bold and role == FontRole.JP, unit_width=fonts.width(part, role, 1.0)))
        out.append(
            Atom(
                AtomKind.LATIN,
                glyphs,
                sum(g.unit_width for g in glyphs),
                no_break_before=word[0] in NO_START,
                no_break_after=word[-1] in NO_END,
            )
        )
        i = j


# 保護要素が上下にはみ出してよい範囲（em）。これを超える分数などは縮めて前後の行に重ならないようにする
_RUN_TOP, _RUN_BOTTOM = 1.1, -0.4


def _run_fit_scale(run: InlineRun) -> float:
    top = max((p.rise_ratio + p.size_ratio * 0.75 for p in run.pieces), default=0.0)
    bottom = min((p.rise_ratio - p.size_ratio * 0.2 for p in run.pieces), default=0.0)
    scale = 1.0
    if top > _RUN_TOP:
        scale = min(scale, _RUN_TOP / top)
    if bottom < _RUN_BOTTOM:
        scale = min(scale, _RUN_BOTTOM / bottom)
    return max(scale, 0.7)


def _inline_atom(run: InlineRun, fonts: FontBook) -> Atom:
    glyphs: list[Glyphs] = []
    positioned = run.width_ratio is not None and all(p.dx_ratio is not None for p in run.pieces)
    fit = _run_fit_scale(run)
    for p in run.pieces:
        if not p.text:
            continue
        if p.math and p.italic:
            base = FontRole.MATH_ITALIC
        elif p.math:
            base = FontRole.MATH
        elif p.italic:
            base = FontRole.LATIN_ITALIC
        elif p.bold:
            base = FontRole.LATIN_BOLD
        else:
            base = FontRole.LATIN
        # 下付き・上付きの大きさとずれは原文の比率を保ちつつ極端な値を抑える
        size_ratio = min(max(p.size_ratio, 0.45), 1.6) * fit
        rise_ratio = min(max(p.rise_ratio, -0.6), 0.8) * fit
        dx = p.dx_ratio * fit if positioned and p.dx_ratio is not None else None
        for part, role in fonts.split_by_coverage(p.text, base):
            w = fonts.width(part, role, 1.0) * size_ratio
            glyphs.append(
                Glyphs(
                    part,
                    role,
                    size_ratio=size_ratio,
                    rise_ratio=rise_ratio,
                    color=p.color,
                    bold=p.bold and role == FontRole.JP,
                    unit_width=w,
                    dx=dx,
                )
            )
            if dx is not None:
                dx += w
    if positioned:
        # 原文の幅を基準にしつつ、代替フォントで幅が広がった場合も重ならないようにする
        natural = max((g.dx or 0.0) + g.unit_width for g in glyphs) if glyphs else 0.0
        return Atom(AtomKind.INLINE, glyphs, max((run.width_ratio or 0.0) * fit, natural), scale=fit)
    return Atom(AtomKind.INLINE, glyphs, sum(g.unit_width for g in glyphs), scale=fit)


def _split_long(atom: Atom, max_unit_width: float, fonts: FontBook) -> list[Atom]:
    """1 行に収まらない長い欧文（URL 等）を文字単位で分割する。"""
    pieces: list[Atom] = []
    cur: list[Glyphs] = []
    cur_w = 0.0
    for g in atom.glyphs:
        for ch in g.text:
            w = fonts.width(ch, g.role, 1.0) * g.size_ratio
            if cur and cur_w + w > max_unit_width:
                pieces.append(Atom(atom.kind, cur, cur_w))
                cur, cur_w = [], 0.0
            if cur and cur[-1].role == g.role and cur[-1].size_ratio == g.size_ratio and cur[-1].rise_ratio == g.rise_ratio:
                cur[-1] = Glyphs(cur[-1].text + ch, g.role, g.size_ratio, g.rise_ratio, g.color, g.bold, cur[-1].unit_width + w)
            else:
                cur.append(Glyphs(ch, g.role, g.size_ratio, g.rise_ratio, g.color, g.bold, w))
            cur_w += w
    if cur:
        pieces.append(Atom(atom.kind, cur, cur_w))
    return pieces


# ---- 行分割 ----------------------------------------------------------------


def _autospace(prev: Atom, cur: Atom) -> float:
    """和欧間の空き（em 単位）。"""
    kinds = {prev.kind, cur.kind}
    if AtomKind.CJK in kinds and kinds & {AtomKind.LATIN, AtomKind.INLINE}:
        cjk = prev if prev.kind == AtomKind.CJK else cur
        if not cjk.cjk_punct:
            return AUTOSPACE_EM
    return 0.0


@dataclass
class _Line:
    atoms: list[Atom]
    natural: float  # em 単位の自然幅（和欧間を含む）
    avail: float  # em 単位の利用可能幅
    last: bool = False


def _measure(atoms: list[Atom]) -> float:
    w = 0.0
    for i, a in enumerate(atoms):
        w += a.unit_width + (_autospace(atoms[i - 1], a) if i else 0.0)
    return w


def break_lines(atoms: list[Atom], widths_em: list[float], fonts: FontBook, overflow_width_em: float) -> list[_Line]:
    """widths_em[i] は i 行目の利用可能幅（em）。範囲外の行は overflow_width_em を使う。"""
    lines: list[_Line] = []
    cur: list[Atom] = []
    cur_w = 0.0
    queue = list(atoms)

    def avail_now() -> float:
        idx = len(lines)
        return widths_em[idx] if idx < len(widths_em) else overflow_width_em

    def finish(next_atoms: list[Atom]) -> None:
        nonlocal cur, cur_w
        while cur and cur[-1].kind == AtomKind.SPACE:
            cur.pop()
        lines.append(_Line(cur, _measure(cur), avail_now()))
        cur = list(next_atoms)
        while cur and cur[0].kind == AtomKind.SPACE:
            cur.pop(0)
        cur_w = _measure(cur)

    i = 0
    while i < len(queue):
        a = queue[i]
        avail = avail_now()
        if not cur and a.kind == AtomKind.SPACE:
            i += 1
            continue
        gap = _autospace(cur[-1], a) if cur else 0.0
        w = a.unit_width + gap
        if cur_w + w <= avail + EPS:
            cur.append(a)
            cur_w += w
            i += 1
            continue
        if not cur:
            # 1 単位だけで行幅を超える：分割するか、分割できなければそのまま置く
            if a.kind == AtomKind.LATIN and len("".join(g.text for g in a.glyphs)) > 1:
                queue[i : i + 1] = _split_long(a, avail, fonts)
                continue
            cur.append(a)
            cur_w += w
            i += 1
            finish([])
            continue
        if a.kind == AtomKind.SPACE:
            i += 1
            finish([])
            continue
        if a.no_break_before and cur_w + w <= avail + 0.5:
            # 追い込み：約物の半角分までは字間を詰めて収める
            cur.append(a)
            cur_w += w
            i += 1
            finish([])
            continue
        carry: list[Atom] = []
        # 追い出し：行頭禁則文字の前の 1 字も一緒に次行へ
        if a.no_break_before and len(cur) > 1 and cur[-1].kind != AtomKind.SPACE:
            carry.insert(0, cur.pop())
        # 行末禁則（開き括弧など）は次行へ送る
        while len(cur) > 1 and cur[-1].no_break_after:
            carry.insert(0, cur.pop())
        finish(carry)
    if cur:
        finish([])
    if lines:
        lines[-1].last = True
    return lines


# ---- 流し込みと縮小 ---------------------------------------------------------


def _frame_capacity(frame: Frame, size: float, pitch: float) -> int:
    h = frame.rect[3] - frame.rect[1] - _top_pad(frame, frame.style)
    if h + size * 0.25 < size:
        return 0
    return 1 + max(0, math.floor((h - size + size * 0.25) / pitch + 1e-6))


def _line_slots(frames: list[Frame], size: float, pitch: float, indent: float) -> list[tuple[int, float]]:
    """各行の (frame 番号, 利用可能幅 pt)。"""
    slots: list[tuple[int, float]] = []
    for fi, fr in enumerate(frames):
        width = fr.rect[2] - fr.rect[0]
        for _ in range(_frame_capacity(fr, size, pitch)):
            w = width - (indent if not slots else 0.0)
            slots.append((fi, max(w, size)))
    return slots


@dataclass
class _Trial:
    lines: list[_Line]
    slots: list[tuple[int, float]]
    size: float
    pitch: float
    fits: bool


def _trial(atoms: list[Atom], frames: list[Frame], style: TextStyle, size: float, pitch: float, fonts: FontBook) -> _Trial:
    indent = style.first_indent * (size / style.size) if style.size else 0.0
    slots = _line_slots(frames, size, pitch, indent)
    widths_em = [w / size for _, w in slots]
    last_width = (frames[-1].rect[2] - frames[-1].rect[0]) / size
    lines = break_lines(atoms, widths_em, fonts, last_width)
    fits = len(lines) <= len(slots) and all(ln.natural <= ln.avail + 0.5 + EPS for ln in lines)
    return _Trial(lines, slots, size, pitch, fits)


def layout(
    tokens: list[Token],
    runs: dict[int, InlineRun],
    frames: list[Frame],
    fonts: FontBook,
    text_scale: float = 0.95,
    min_scale: float = 0.2,
) -> LayoutResult:
    """訳文を frames に流し込み、配置済みグリフを返す。"""
    if not frames:
        return LayoutResult([], 1.0, False, 0)
    style = frames[0].style
    atoms = build_atoms(tokens, runs, fonts, style.bold)
    if not atoms:
        return LayoutResult([], 1.0, True, 0)

    base_size = style.size * text_scale
    # 行送りは原文を基準に、和文として最低限の行間（1.15 倍）を確保する
    base_pitch = max(style.line_pitch, base_size * 1.15)

    def attempt(scale: float) -> _Trial:
        return _trial(atoms, frames, style, base_size * scale, base_pitch * scale, fonts)

    best = attempt(1.0)
    scale = 1.0
    if not best.fits:
        lo, hi = min_scale, 1.0
        low_trial = attempt(lo)
        if low_trial.fits:
            best = low_trial
            scale = lo
            for _ in range(14):
                mid = (lo + hi) / 2
                t = attempt(mid)
                if t.fits:
                    lo, best, scale = mid, t, mid
                else:
                    hi = mid
                if hi - lo < 0.004:
                    break
        else:
            best, scale = low_trial, lo

    placed, used, placed_runs = _place(best, frames, style)
    return LayoutResult(placed, scale * text_scale, best.fits, len(best.lines), used, placed_runs)


def _place(trial: _Trial, frames: list[Frame], style: TextStyle) -> tuple[list[PlacedGlyphs], list[Rect], list[PlacedRun]]:
    size, pitch = trial.size, trial.pitch
    placed: list[PlacedGlyphs] = []
    placed_runs: list[PlacedRun] = []
    used: list[Rect] = []
    frame_line_index: dict[int, int] = {}
    indent = style.first_indent * (size / style.size) if style.size else 0.0
    for li, line in enumerate(trial.lines):
        if li >= len(trial.slots):
            break  # 収まらない行は描かない（fits=False として報告済み）
        fi, avail_pt = trial.slots[li]
        fr = frames[fi]
        k = frame_line_index.get(fi, 0)
        frame_line_index[fi] = k + 1
        top_pad = _top_pad(fr, style)
        baseline = fr.rect[1] + top_pad + ASCENT * size + k * pitch
        x0 = fr.rect[0] + (indent if li == 0 else 0.0)

        natural_pt = line.natural * size
        gaps = len(line.atoms) - 1
        extra = avail_pt - natural_pt
        align = fr.style.align
        gap_add = 0.0
        if align == Align.JUSTIFY and not line.last and gaps > 0:
            gap_add = extra / gaps
        elif extra < 0 and gaps > 0:
            gap_add = extra / gaps  # 追い込み分を字間で吸収
        elif align == Align.CENTER:
            x0 += max(0.0, extra) / 2

        x = x0
        for ai, atom in enumerate(line.atoms):
            if ai:
                x += _autospace(line.atoms[ai - 1], atom) * size + gap_add
            atom_x = x
            if atom.run_id is not None:
                factor = (size / style.size if style.size else 1.0) * atom.scale
                placed_runs.append(PlacedRun(atom.run_id, fr.page, atom_x, baseline, factor))
            for g in atom.glyphs:
                gsize = size * g.size_ratio
                gx = atom_x + g.dx * size if g.dx is not None else x
                if atom.kind != AtomKind.SPACE:
                    placed.append(
                        PlacedGlyphs(
                            page=fr.page,
                            x=gx,
                            y=baseline - g.rise_ratio * size,
                            text=g.text,
                            role=g.role,
                            size=gsize,
                            color=style.color if g.color is None else g.color,
                            bold=g.bold,
                            run_id=atom.run_id,
                        )
                    )
                x = gx + g.unit_width * size
            x = max(x, atom_x + atom.unit_width * size)
        line_right = min(x, fr.rect[2])
        rect = (fr.rect[0], baseline - ASCENT * size, max(line_right, fr.rect[0]), baseline + DESCENT * size)
        if len(used) <= fi:
            used.extend([None] * (fi + 1 - len(used)))  # type: ignore[list-item]
        prev = used[fi]
        used[fi] = rect if prev is None else (min(prev[0], rect[0]), min(prev[1], rect[1]), max(prev[2], rect[2]), max(prev[3], rect[3]))
    return placed, [u for u in used if u is not None], placed_runs


def _top_pad(frame: Frame, style: TextStyle) -> float:
    """原文の 1 行目の位置に合わせるための上余白（大きくなりすぎないよう制限）。"""
    if frame.first_baseline_offset is None:
        return 0.0
    pad = frame.first_baseline_offset - ASCENT * style.size
    return min(max(pad, 0.0), style.size * 0.3)
