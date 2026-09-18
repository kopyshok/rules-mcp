"""Извлечение запросов 1С из текста обработчика правил обмена.

Только stdlib, без зависимостей от `rules_index` и `server`. Вход — текст
обработчика строкой ровно в том виде, в каком его отдаёт индекс: нумерация строк
идёт от 1 по `handler_text.splitlines()`, своей нормализации текста нет.

Разбор намеренно консервативный: неизвестная конструкция помечается
(`complete=False` + `reason`), а не достраивается догадкой. Исключений наружу
модуль не выбрасывает.

Единственная публичная функция — `extract_queries`.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Модель


@dataclass(slots=True)
class Param:
    """Параметр запроса, установленный через `УстановитьПараметр`."""

    name: str  # имя параметра как написано в кавычках
    expression: str  # выражение значения, как в коде
    line: int  # номер строки в тексте обработчика, от 1


@dataclass(slots=True)
class PacketQuery:
    """Один запрос пакета — то, что лежит между `;` верхнего уровня."""

    index: int  # позиция в пакете, с нуля
    text: str  # текст этого запроса пакета
    temp_table: str  # имя после ПОМЕСТИТЬ, иначе ""
    depends_on: list[str] = field(default_factory=list)  # ВТ пакета, читаемые запросом
    result_vars: list[str] = field(default_factory=list)  # кому присвоен Результат[index]


@dataclass(slots=True)
class UnresolvedResult:
    """Обращение к элементу пакета по вычисляемому номеру — номер не угадываем."""

    variable: str
    expression: str  # то, что стоит в скобках
    line: int


@dataclass(slots=True)
class QueryUse:
    """Одно использование запроса: от присваивания текста до его выполнения."""

    variable: str  # имя переменной запроса, например "Запрос"
    assign_line: int  # строка, где задан текст
    exec_line: int = 0  # строка вызова выполнения, 0 если не найден
    # "ВыполнитьПакет" | "ВыполнитьПакетСПромежуточнымиДанными" | "Выполнить" | ""
    exec_kind: str = ""
    text: str | None = None  # полный текст запроса, None если не извлечён
    params: list[Param] = field(default_factory=list)
    packet: list[PacketQuery] = field(default_factory=list)
    unresolved: list[UnresolvedResult] = field(default_factory=list)
    complete: bool = True
    reason: str = ""  # заполняется только при complete=False


# ---------------------------------------------------------------------------
# Лексический разбор кода 1С
#
# Текст обработчика превращается в «маску»: то же число строк и та же разбивка
# по строкам, но комментарии вырезаны, а каждый строковый литерал заменён
# меткой \x00N\x00. После этого многострочный литерал перестаёт мешать
# регулярным выражениям, а номера строк остаются исходными.

_MARK = "\x00"
_MARK_RE = re.compile(r"\x00(\d+)\x00")
_IDENT = r"[^\W\d]\w*"
_REGION = ("#Область", "#КонецОбласти")


def _scan(text: str) -> tuple[str, list[tuple[str, int]]]:
    """Маска текста обработчика и список литералов `(значение, строка начала)`.

    Комментарии (строка, начинающаяся с `//` после отступа, и хвост строки после
    `//` вне литерала) в маску не попадают. В многострочном литерале снимается
    ведущий `|`, удвоенная кавычка `""` превращается в одну, а закомментированные
    строки внутри литерала выбрасываются — так же, как их видит сам 1С.
    """
    lines = text.splitlines()
    masked: list[str] = []
    literals: list[tuple[str, int]] = []
    in_str = False
    cur: list[str] = []
    cur_line = 0

    for no, raw in enumerate(lines, 1):
        piece: list[str] = []
        pos = 0
        if raw.lstrip().startswith("//"):
            # Комментарий: и в коде, и внутри многострочного литерала.
            masked.append("")
            continue
        if not in_str and raw.lstrip().startswith(_REGION):
            # Разметка областей — не выражение: её ставят и между `=` и литералом.
            masked.append("")
            continue
        if in_str:
            # Продолжение литерала: снимаем отступ и ведущий `|`.
            pos = len(raw) - len(raw.lstrip())
            if pos < len(raw) and raw[pos] == "|":
                pos += 1
            cur.append("\n")

        while pos < len(raw):
            ch = raw[pos]
            if in_str:
                if ch == '"':
                    if pos + 1 < len(raw) and raw[pos + 1] == '"':
                        cur.append('"')
                        pos += 2
                        continue
                    in_str = False
                    literals.append(("".join(cur), cur_line))
                    piece.append(f"{_MARK}{len(literals) - 1}{_MARK}")
                    cur = []
                    pos += 1
                    continue
                cur.append(ch)
                pos += 1
                continue
            if ch == '"':
                in_str = True
                cur = []
                cur_line = no
                pos += 1
                continue
            if ch == "/" and pos + 1 < len(raw) and raw[pos + 1] == "/":
                break  # хвостовой комментарий
            piece.append(ch)
            pos += 1
        masked.append("".join(piece))

    if in_str and masked:
        # Литерал не закрыт — текст оборван. Метку не ставим: присваивание
        # останется нераспознанным и честно получит complete=False.
        literals.append(("".join(cur), cur_line))
    return "\n".join(masked), literals


def _render(value: str) -> str:
    """Значение литерала обратно в вид исходного кода."""
    return '"' + value.replace('"', '""') + '"'


def _unmask(fragment: str, literals: list[tuple[str, int]]) -> str:
    """Возвращает метки литералов обратно в текст выражения."""

    def back(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return _render(literals[idx][0]) if idx < len(literals) else ""

    return _MARK_RE.sub(back, fragment).strip()


def _line_starts(masked: str) -> list[int]:
    starts = [0]
    for pos, ch in enumerate(masked):
        if ch == "\n":
            starts.append(pos + 1)
    return starts


def _line_of(starts: list[int], pos: int) -> int:
    return bisect_right(starts, pos)


def _statement_start(masked: str, pos: int) -> bool:
    """Позиция начинает оператор: слева до края строки только пробелы или `;`."""
    idx = pos - 1
    while idx >= 0 and masked[idx] in " \t":
        idx -= 1
    return idx < 0 or masked[idx] in ";\n"


def _call_args(masked: str, open_pos: int) -> tuple[list[str], int] | None:
    """Аргументы вызова, начиная с открывающей скобки, с учётом вложенности.

    Возвращает `(аргументы, позиция закрывающей скобки)` или None, если скобка
    не закрылась.
    """
    depth = 0
    args: list[str] = []
    cur: list[str] = []
    pos = open_pos
    while pos < len(masked):
        ch = masked[pos]
        if ch in "([":
            depth += 1
            if depth == 1 and ch == "(":
                pos += 1
                continue
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                args.append("".join(cur))
                return args, pos
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        pos += 1
    return None


def _literal_index(fragment: str) -> int | None:
    """Номер литерала, если фрагмент — ровно одна метка и ничего больше."""
    match = _MARK_RE.fullmatch(fragment.strip())
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Лексический разбор языка запросов


def _bare(text: str) -> str:
    """Текст запроса той же длины, где строки и комментарии заменены пробелами.

    Нужен только для поиска `;`, `ПОМЕСТИТЬ` и источников — сам текст запроса
    модуль не трогает.
    """
    out = list(text)
    pos, size, in_str = 0, len(text), False
    while pos < size:
        ch = text[pos]
        if in_str:
            if ch == '"':
                if pos + 1 < size and text[pos + 1] == '"':
                    out[pos] = out[pos + 1] = " "
                    pos += 2
                    continue
                in_str = False
            if ch != "\n":
                out[pos] = " "
            pos += 1
            continue
        if ch == '"':
            in_str = True
            out[pos] = " "
            pos += 1
            continue
        if ch == "/" and pos + 1 < size and text[pos + 1] == "/":
            end = text.find("\n", pos)
            end = size if end == -1 else end
            for k in range(pos, end):
                out[k] = " "
            pos = end
            continue
        pos += 1
    return "".join(out)


_PLACE_RE = re.compile(r"\bПОМЕСТИТЬ\s+(" + _IDENT + r")", re.IGNORECASE)
_SOURCE_RE = re.compile(r"\b(?:ИЗ|СОЕДИНЕНИЕ)\s+(" + _IDENT + r")", re.IGNORECASE)
_PREFIX_RE = re.compile(r"\b(" + _IDENT + r")\s*\.")


def _split_packet(text: str) -> list[tuple[str, str]]:
    """Делит текст на запросы пакета по `;` верхнего уровня.

    Возвращает пары `(текст запроса, тот же текст без строк и комментариев)`.
    `УНИЧТОЖИТЬ <Имя>` — такой же элемент пакета, как остальные: номера не
    сдвигаются.
    """
    bare = _bare(text)
    parts: list[tuple[str, str]] = []
    start = 0
    for pos, ch in enumerate(bare):
        if ch == ";":
            parts.append((text[start:pos], bare[start:pos]))
            start = pos + 1
    tail = text[start:]
    if tail.strip():
        parts.append((tail, bare[start:]))
    return [(raw.strip(), bare_part) for raw, bare_part in parts]


def _build_packet(text: str) -> list[PacketQuery]:
    """Пакет запросов: номер, временная таблица, зависимости от ранних таблиц."""
    packet: list[PacketQuery] = []
    created: list[str] = []
    for index, (raw, bare) in enumerate(_split_packet(text)):
        place = _PLACE_RE.search(bare)
        temp_table = place.group(1) if place else ""
        known = {name.casefold(): name for name in created}
        used: set[str] = set()
        for pattern in (_SOURCE_RE, _PREFIX_RE):
            for match in pattern.finditer(bare):
                name = known.get(match.group(1).casefold())
                if name is not None and name.casefold() != temp_table.casefold():
                    used.add(name)
        packet.append(
            PacketQuery(
                index=index,
                text=raw,
                temp_table=temp_table,
                depends_on=sorted(used),
            )
        )
        if temp_table:
            created.append(temp_table)
    return packet


# ---------------------------------------------------------------------------
# Разбор обработчика

_TEXT_RE = re.compile(r"(" + _IDENT + r")\s*\.\s*Текст\s*=\s*([^;]*)(?:;|\Z)")
_NEW_RE = re.compile(r"(" + _IDENT + r")\s*=\s*Новый\s+Запрос\s*\(")
_PARAM_RE = re.compile(r"(" + _IDENT + r")\s*\.\s*УстановитьПараметр\s*\(")
# Оба способа возвращают массив, пронумерованный по порядку запросов в пакете,
# поэтому привязка Результат[n] к элементу пакета у них одинаковая.
_PACKET_KINDS = ("ВыполнитьПакетСПромежуточнымиДанными", "ВыполнитьПакет")
_EXEC_RE = re.compile(
    r"(?:(" + _IDENT + r")\s*=\s*)?(" + _IDENT + r")\s*\.\s*("
    + "|".join((*_PACKET_KINDS, "Выполнить"))
    + r")\s*\(\s*\)"
)
_INDEX_RE = re.compile(r"(" + _IDENT + r")\s*=\s*(" + _IDENT + r")\s*\[([^\]\n]*)\]")

_REASON_VAR = "текст запроса задан переменной, значение статически неизвестно"


def _reason_for(expr: str) -> str:
    """Причина неполноты по виду правой части присваивания."""
    short = " ".join(expr.split())
    if len(short) > 80:
        short = short[:77] + "..."
    if re.fullmatch(_IDENT, short) or re.fullmatch(_IDENT + r"(?:\." + _IDENT + r")+", short):
        return _REASON_VAR
    if not short:
        return "текст запроса не задан, значение статически неизвестно"
    return f"текст запроса не сводится к одному строковому литералу: «{short}»"


def _is_append(variable: str, right: str) -> bool:
    """Правая часть читает текст той же переменной — это дозапись, а не новый текст."""
    return re.search(r"\b" + re.escape(variable) + r"\s*\.\s*Текст\b", right) is not None


def _mark_appends(uses: list[QueryUse], appends: list[tuple[str, int]]) -> None:
    """Присваивание, к тексту которого ниже дописывают, полным быть не может.

    Выполняется другой текст: базовый плюс то, что доклеено в цикле или ветке.
    Отдавать базовый как полный — ровно тот частичный текст, который запрещён.
    """
    for variable, line in appends:
        before = [use for use in uses if use.variable == variable and use.assign_line < line]
        if not before or not before[-1].complete:
            continue
        target = before[-1]
        target.complete = False
        target.text = None
        target.reason = (
            f"текст запроса дополняется ниже, на строке {line}: "
            "итоговое значение статически неизвестно"
        )


def _collect_assignments(
    masked: str, literals: list[tuple[str, int]], starts: list[int]
) -> list[QueryUse]:
    """Все присваивания текста запроса: `.Текст = …` и `Новый Запрос(…)`."""
    uses: list[QueryUse] = []
    appends: list[tuple[str, int]] = []

    for match in _TEXT_RE.finditer(masked):
        if not _statement_start(masked, match.start()):
            continue  # например, сравнение `Если Запрос.Текст = "" Тогда`
        line = _line_of(starts, match.start())
        idx = _literal_index(match.group(2))
        if idx is not None:
            uses.append(QueryUse(variable=match.group(1), assign_line=line, text=literals[idx][0]))
        else:
            if _is_append(match.group(1), match.group(2)):
                appends.append((match.group(1), line))
            uses.append(
                QueryUse(
                    variable=match.group(1),
                    assign_line=line,
                    text=None,
                    complete=False,
                    reason=_reason_for(_unmask(match.group(2), literals)),
                )
            )

    for match in _NEW_RE.finditer(masked):
        if not _statement_start(masked, match.start()):
            continue
        parsed = _call_args(masked, match.end() - 1)
        if parsed is None:
            continue
        args = [a for a in parsed[0] if a.strip()]
        if not args:
            continue  # `Новый Запрос;` или `Новый Запрос()` — текста ещё нет
        line = _line_of(starts, match.start())
        idx = _literal_index(args[0])
        if idx is not None:
            uses.append(QueryUse(variable=match.group(1), assign_line=line, text=literals[idx][0]))
        else:
            uses.append(
                QueryUse(
                    variable=match.group(1),
                    assign_line=line,
                    text=None,
                    complete=False,
                    reason=_reason_for(_unmask(args[0], literals)),
                )
            )

    uses.sort(key=lambda use: use.assign_line)
    _mark_appends(uses, appends)
    return uses


def _owner(uses: list[QueryUse], variable: str, line: int, allow_later: bool) -> QueryUse | None:
    """Присваивание, к которому относится оператор на строке `line`.

    Основное правило — последнее присваивание той же переменной, предшествующее
    строке. `allow_later` нужен для `УстановитьПараметр`: параметры часто
    выставляют до того, как задан текст, и к моменту выполнения они всё равно
    относятся к первому следующему запросу этой переменной.
    """
    own = [use for use in uses if use.variable == variable]
    before = [use for use in own if use.assign_line <= line]
    if before:
        return before[-1]
    if allow_later and own:
        return own[0]
    return None


def _scope_end(uses: list[QueryUse], use: QueryUse) -> int:
    """Строка следующего присваивания той же переменной; дальше запрос уже другой."""
    later = [
        other.assign_line
        for other in uses
        if other.variable == use.variable and other.assign_line > use.assign_line
    ]
    return min(later) if later else 1 << 30


def extract_queries(handler_text: str) -> list[QueryUse]:
    """Запросы, встречающиеся в тексте обработчика, в порядке задания текста.

    Новое присваивание текста той же переменной начинает новый `QueryUse`.
    Параметры и вызов выполнения относятся к последнему присваиванию, которое их
    предшествует по номеру строки. Обращения `Результат[<число>]` привязываются
    к элементу пакета, `Результат[<выражение>]` — попадают в `unresolved`.

    Если ниже по тексту к переменной есть дозапись (`X.Текст = X.Текст + …`),
    предшествующее присваивание тоже становится неполным: выполняется не оно.

    Исключений не выбрасывает: любая неожиданная конструкция даёт
    `complete=False`, `text=None` и заполненный `reason`.
    """
    if not handler_text:
        return []

    masked, literals = _scan(handler_text)
    starts = _line_starts(masked)
    uses = _collect_assignments(masked, literals, starts)
    if not uses:
        return []

    # Параметры.
    for match in _PARAM_RE.finditer(masked):
        if not _statement_start(masked, match.start()):
            continue
        parsed = _call_args(masked, match.end() - 1)
        if parsed is None:
            continue
        args = parsed[0]
        if len(args) < 2:
            continue
        line = _line_of(starts, match.start())
        use = _owner(uses, match.group(1), line, allow_later=True)
        if use is None:
            continue
        idx = _literal_index(args[0])
        name = literals[idx][0] if idx is not None else _unmask(args[0], literals)
        use.params.append(
            Param(name=name, expression=_unmask(",".join(args[1:]), literals), line=line)
        )

    # Выполнение.
    result_var: dict[int, str] = {}
    for match in _EXEC_RE.finditer(masked):
        if not _statement_start(masked, match.start()):
            continue
        line = _line_of(starts, match.start())
        use = _owner(uses, match.group(2), line, allow_later=False)
        if use is None or use.exec_kind:
            continue
        use.exec_line = line
        use.exec_kind = match.group(3)
        if match.group(1):
            result_var[id(use)] = match.group(1)

    # Пакет и привязка результатов.
    for use in uses:
        if use.text is None:
            continue
        use.packet = _build_packet(use.text)
        name = result_var.get(id(use), "")
        if use.exec_kind not in _PACKET_KINDS or not name:
            continue
        end = _scope_end(uses, use)
        by_index = {item.index: item for item in use.packet}
        for match in _INDEX_RE.finditer(masked):
            if match.group(2) != name or not _statement_start(masked, match.start()):
                continue
            line = _line_of(starts, match.start())
            if not use.exec_line <= line < end:
                continue
            inner = _unmask(match.group(3), literals)
            if inner.isdigit() and int(inner) in by_index:
                by_index[int(inner)].result_vars.append(match.group(1))
            else:
                use.unresolved.append(
                    UnresolvedResult(variable=match.group(1), expression=inner, line=line)
                )

    return uses
