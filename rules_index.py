"""Разбор правил обмена 1С (Конвертация данных 2.0) и индекс для запросов.

Читает тройку файлов направления:
    ExchangeRules.xml      — ПВД (правила выгрузки) + ПКО (правила конвертации)
    RegistrationRules.xml  — ПРО (правила регистрации)
    CorrespondentExchangeRules.xml — правила корреспондента (не индексируются)

Только stdlib. Разбор всего корпуса (36 МБ, шесть направлений) — около секунды.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Модель


@dataclass(slots=True)
class Prop:
    """ПКС — правило конвертации свойства (реквизит источника → реквизит приёмника)."""

    code: str
    name: str
    src: str  # пусто = заполняется алгоритмом, а не переносится
    src_type: str
    dst: str
    dst_type: str
    dst_kind: str  # Реквизит / Свойство / ...
    conv_rule: str  # подчинённое ПКО (каскад)
    search: bool
    disabled: bool
    handler: str  # текст ПередВыгрузкой
    param: str = ""  # ИмяПараметраДляПередачи: значение уходит параметром, а не в реквизит


@dataclass(slots=True)
class Section:
    """Табличная часть внутри ПКО."""

    name: str
    src: str
    dst: str
    props: list[Prop]


@dataclass(slots=True)
class Pko:
    """ПКО — правило конвертации объектов."""

    code: str
    name: str
    src: str
    dst: str
    disabled: bool
    props: list[Prop]
    sections: list[Section]
    handlers: dict[str, str]
    blocks: list[str]  # функциональные блоки, которыми помечено правило


@dataclass(slots=True)
class Pvd:
    """ПВД — правило выгрузки данных."""

    code: str
    name: str
    obj: str  # ОбъектВыборки, например ДокументСсылка.ПоступлениеТоваровУслуг
    conv_rule: str
    disabled: bool
    handlers: dict[str, str]
    pko_calls: list[str]
    blocks: list[str]  # функциональные блоки, которыми помечены ветки
    selection: str = "СтандартнаяВыборка"  # способ отбора данных

    @property
    def custom_selection(self) -> bool:
        """Отбор задан запросом в обработчике, а не объектом выборки.

        У таких веток ОбъектВыборки декоративен и врёт: в боевых правилах
        встречаются ветки, числящиеся за совсем посторонним документом.
        """
        return self.selection == "ПроизвольныйАлгоритм"


@dataclass(slots=True)
class Pro:
    """ПРО — правило регистрации объектов."""

    code: str
    name: str
    obj: str  # ОбъектНастройки
    disabled: bool
    plan_filter: str  # отбор по свойствам плана обмена, человекочитаемо
    obj_filter: str  # отбор по свойствам объекта, человекочитаемо
    handlers: dict[str, str]
    vidops: list[str]  # значения ВидОперации, встречающиеся в отборе


@dataclass(slots=True)
class Snippet:
    """Общий алгоритм или именованный запрос — текст, живущий вне правил.

    В вызовах ПКО он участвует наравне с обработчиками: ветка выгрузки делает
    `Выполнить(Алгоритмы.Имя)`, а внутри алгоритма стоит ВыгрузитьПоПравилу.
    Поле handlers с единственным ключом «Текст» — чтобы чтение и поиск работали
    так же, как для правил, без отдельной ветки кода.
    """

    code: str
    name: str
    disabled: bool
    handlers: dict[str, str]
    group: str  # Выгрузка / Загрузка — только у алгоритмов
    pko_calls: list[str]


@dataclass
class Direction:
    """Одно направление обмена — папка с правилами."""

    key: str  # имя папки, например УПП_ERP
    path: Path
    src_config: str  # синоним конфигурации-источника из самих правил
    dst_config: str
    pro: list[Pro] = field(default_factory=list)
    pvd: list[Pvd] = field(default_factory=list)
    pko: dict[str, Pko] = field(default_factory=dict)
    algorithms: dict[str, Snippet] = field(default_factory=dict)
    queries: dict[str, Snippet] = field(default_factory=dict)
    raw: str = ""  # текст ExchangeRules.xml для полнотекстового поиска
    raw_reg: str = ""

    @property
    def title(self) -> str:
        return f"{short_config(self.src_config)} → {short_config(self.dst_config)}"


# ---------------------------------------------------------------------------
# Мелкие помощники

_ENUM_RE = re.compile(r"Перечисления\.[А-Яа-яA-Za-z_0-9]+\.([А-Яа-яA-Za-z_0-9]+)")
_VIDOP_RE = re.compile(r"Перечисления\.ВидыОпераций[А-Яа-я_]*\.([А-Яа-яA-Za-z_0-9]+)")
_IMYA_PKO_RE = re.compile(r'ИмяПКО\s*=\s*"([^"]+)"')
_ALG_CALL_RE = re.compile(r"Алгоритмы\.([А-Яа-яA-Za-z_0-9]+)")
_VYGR_RE = re.compile(r'ВыгрузитьПоПравилу\s*\((?:[^()]|\([^()]*\))*?"([^"]+)"')
_BLOCK_RES = (
    re.compile(r'_ФБ\s*=\s*"([^"]+)"'),
    re.compile(r'"ФункциональныйБлок"\s*,\s*"([^"]+)"'),
    re.compile(r"сл_ФункциональныеБлоки\.([А-Яа-яA-Za-z_0-9]+)"),
)

# Короткие имена конфигураций для заголовков и синонимов направления.
_CONFIG_SHORT = (
    ("производственным предприятием", "УПП"),
    ("ERP", "ЕРП"),
    ("Розница", "Розница"),
    ("Retail", "Розница"),
    ("Итилиум", "Итилиум"),
)


def short_config(synonym: str) -> str:
    for needle, short in _CONFIG_SHORT:
        if needle.lower() in synonym.lower():
            return short
    return synonym.split(",")[0].strip() or "?"


def obj_short(ref: str) -> str:
    """`ДокументСсылка.ПоступлениеТоваровУслуг` → `ПоступлениеТоваровУслуг`."""
    return ref.rsplit(".", 1)[-1] if ref else ""


def obj_kind(ref: str) -> str:
    """`ДокументСсылка.X` → `Документ`."""
    head = ref.split(".", 1)[0] if ref else ""
    return head.replace("Ссылка", "") or "?"


def _disabled(el: ET.Element) -> bool:
    return el.get("Отключить", "false") == "true"


def _text(el: ET.Element | None, tag: str, default: str = "") -> str:
    if el is None:
        return default
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _uncommented(code: str) -> str:
    """Убирает строки, закомментированные в коде 1С."""
    return "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("//"))


def _extract_blocks(code: str) -> list[str]:
    """Функциональные блоки, которыми разработчики помечают ветки выгрузки."""
    active = _uncommented(code)
    found: set[str] = set()
    for pattern in _BLOCK_RES:
        found.update(pattern.findall(active))
    return sorted(found)


def _walk_rules(node: ET.Element | None, inherited_off: bool = False):
    """Обходит дерево Группа/Правило, помня отключённость групп-родителей."""
    if node is None:
        return
    for child in node:
        off = inherited_off or _disabled(child)
        if child.tag == "Правило":
            yield child, off
        elif child.tag == "Группа":
            yield from _walk_rules(child, off)


def _handlers(el: ET.Element, names: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        node = el.find(name)
        if node is not None and (node.text or "").strip():
            out[name] = node.text.strip()
    return out


# ---------------------------------------------------------------------------
# Отбор ПРО → человекочитаемый вид


def _clean_value(raw: str) -> str:
    """`Значение = Перечисления.ВидыОперацийX.ПокупкаКомиссия;` → `ПокупкаКомиссия`."""
    value = raw.strip().removeprefix("Значение").lstrip("= ").rstrip(";").strip()
    match = _ENUM_RE.fullmatch(value)
    if match:
        return match.group(1)
    if value in ("true", "false"):
        return "Истина" if value == "true" else "Ложь"
    return value


_COMPARE = {
    "Равно": "=",
    "НеРавно": "<>",
    "Больше": ">",
    "БольшеИлиРавно": ">=",
    "Меньше": "<",
    "МеньшеИлиРавно": "<=",
    "ВСписке": "в списке",
    "НеВСписке": "не в списке",
}


def _render_filter(node: ET.Element | None, depth: int = 0) -> list[str]:
    """Разворачивает дерево отбора в отступами оформленные строки."""
    if node is None:
        return []
    lines: list[str] = []
    pad = "  " * depth
    for child in node:
        if child.tag == "ЭлементОтбора":
            prop = _text(child, "СвойствоОбъекта") or _text(child, "СвойствоПланаОбмена")
            cmp_ = _COMPARE.get(_text(child, "ВидСравнения"), _text(child, "ВидСравнения"))
            value = _clean_value(_text(child, "ЗначениеКонстанты"))
            lines.append(f"{pad}{prop} {cmp_} {value}".rstrip())
        elif child.tag == "Группа":
            boolean = _text(child, "БулевоЗначениеГруппы", "И")
            inner = _render_filter(child, depth + 1)
            if inner:
                lines.append(f"{pad}({boolean}:")
                lines.extend(inner)
                lines.append(f"{pad})")
    return lines


# ---------------------------------------------------------------------------
# Разбор ПКО


_PROP_HANDLERS = ("ПередВыгрузкой",)
_PKO_HANDLERS = ("ПередВыгрузкой", "ПриВыгрузке", "ПослеВыгрузки", "ПриЗагрузке", "ПослеЗагрузки")


def _parse_prop(el: ET.Element, inherited_off: bool) -> Prop:
    src = el.find("Источник")
    dst = el.find("Приемник")
    return Prop(
        code=_text(el, "Код"),
        name=_text(el, "Наименование"),
        src=(src.get("Имя", "") if src is not None else ""),
        src_type=(src.get("Тип", "") if src is not None else ""),
        dst=(dst.get("Имя", "") if dst is not None else ""),
        dst_type=(dst.get("Тип", "") if dst is not None else ""),
        dst_kind=(dst.get("Вид", "") if dst is not None else ""),
        conv_rule=_text(el, "КодПравилаКонвертации"),
        search=el.get("Поиск", "false") == "true",
        disabled=inherited_off or _disabled(el),
        handler=_text(el, "ПередВыгрузкой"),
        param=_text(el, "ИмяПараметраДляПередачи"),
    )


def _parse_pko(el: ET.Element, off: bool) -> Pko:
    props: list[Prop] = []
    sections: list[Section] = []
    container = el.find("Свойства")
    if container is not None:
        for child in container:
            if child.tag == "Свойство":
                props.append(_parse_prop(child, off))
            elif child.tag == "Группа":
                sec_src = child.find("Источник")
                sec_dst = child.find("Приемник")
                sections.append(
                    Section(
                        name=_text(child, "Наименование"),
                        src=(sec_src.get("Имя", "") if sec_src is not None else ""),
                        dst=(sec_dst.get("Имя", "") if sec_dst is not None else ""),
                        props=[
                            _parse_prop(p, off or _disabled(child))
                            for p in child.findall("Свойство")
                        ],
                    )
                )
    handlers = _handlers(el, _PKO_HANDLERS)
    return Pko(
        code=_text(el, "Код"),
        name=_text(el, "Наименование"),
        src=_text(el, "Источник"),
        dst=_text(el, "Приемник"),
        disabled=off,
        props=props,
        sections=sections,
        handlers=handlers,
        blocks=_extract_blocks("\n".join(handlers.values())),
    )


# ---------------------------------------------------------------------------
# Разбор ПВД


_PVD_HANDLERS = (
    "ПередОбработкойПравила",
    "ПередВыгрузкойОбъекта",
    "ПослеВыгрузкиОбъекта",
    "ПослеОбработкиПравила",
)


def _pko_calls(code: str) -> list[str]:
    """Имена ПКО, вызываемых из текста. Закомментированное не считается."""
    active = _uncommented(code)
    return sorted(set(_IMYA_PKO_RE.findall(active)) | set(_VYGR_RE.findall(active)))


def _lines_of(code: str, patterns: tuple[re.Pattern, ...]) -> dict[str, list[int]]:
    """Имя из первой группы выражения → номера строк, в нумерации page_lines.

    Закомментированные строки опустошаются, а не выбрасываются: номера не
    сдвигаются. Номер — строка, где стоит само имя, поэтому вызов, разнесённый
    на несколько строк, указывает на строку с именем правила.
    """
    live = "\n".join("" if ln.lstrip().startswith("//") else ln for ln in code.splitlines())
    out: dict[str, set[int]] = {}
    for pattern in patterns:
        for match in pattern.finditer(live):
            out.setdefault(match.group(1), set()).add(live.count("\n", 0, match.start(1)) + 1)
    return {name: sorted(lines) for name, lines in sorted(out.items())}


def pko_call_lines(code: str) -> dict[str, list[int]]:
    """Имя ПКО → строки вызовов. Те же выражения, что у _pko_calls."""
    return _lines_of(code, (_IMYA_PKO_RE, _VYGR_RE))


def call_places(rule, kind: str, pko: str) -> list[dict]:
    """Где в обработчиках правила вызывается ПКО: обработчик и строка, по порядку выполнения."""
    return [
        {"обработчик": name, "строка": line}
        for name in handler_names(kind, rule.handlers)
        for line in pko_call_lines(rule.handlers[name]).get(pko, [])
    ]


def branch_call_places(pvd: Pvd, pko: str) -> dict:
    """Поля ответа trace_document о месте вызова ПКО веткой.

    Правило, заданное в свойствах ветки, строки в коде не имеет — это
    говорится словами, а не пустым списком. У 33 веток корпуса правило
    задано в свойствах и вдобавок вызывается в коде: тогда список и пометка.
    """
    places = call_places(pvd, "ПВД", pko)
    in_props = pko == pvd.conv_rule
    if not places:
        return {
            "где вызывается": "в коде не вызывается — задано в свойствах ветки, строки нет"
            if in_props
            else "строка вызова не найдена"
        }
    return {"где вызывается": places, **({"задано и в свойствах ветки": True} if in_props else {})}


def _parse_pvd(el: ET.Element, off: bool) -> Pvd:
    handlers = _handlers(el, _PVD_HANDLERS)
    joined = "\n".join(handlers.values())
    calls = _pko_calls(joined)
    if _text(el, "КодПравилаКонвертации"):
        calls = sorted(set(calls) | {_text(el, "КодПравилаКонвертации")})
    blocks = _extract_blocks(joined)
    return Pvd(
        code=_text(el, "Код"),
        name=_text(el, "Наименование"),
        obj=_text(el, "ОбъектВыборки"),
        conv_rule=_text(el, "КодПравилаКонвертации"),
        disabled=off,
        handlers=handlers,
        pko_calls=calls,
        blocks=sorted(blocks),
        selection=_text(el, "СпособОтбораДанных", "СтандартнаяВыборка"),
    )


# ---------------------------------------------------------------------------
# Разбор ПРО


_PRO_HANDLERS = ("ПередОбработкой", "ПриОбработке", "ПриОбработкеДополнительный", "ПослеОбработки")


def _parse_pro(el: ET.Element, off: bool) -> Pro:
    obj_filter = "\n".join(_render_filter(el.find("ОтборПоСвойствамОбъекта")))
    plan_filter = "\n".join(_render_filter(el.find("ОтборПоСвойствамПланаОбмена")))
    return Pro(
        code=_text(el, "Код"),
        name=_text(el, "Наименование"),
        obj=_text(el, "ОбъектНастройки"),
        disabled=off,
        plan_filter=plan_filter,
        obj_filter=obj_filter,
        handlers=_handlers(el, _PRO_HANDLERS),
        vidops=sorted(set(_VIDOP_RE.findall(ET.tostring(el, encoding="unicode")))),
    )


# ---------------------------------------------------------------------------
# Чтение текста обработчиков

RULE_KINDS = ("ПВД", "ПКО", "ПРО", "Алгоритм", "Запрос")

#: Вид правила → имена его обработчиков в том порядке, в каком они выполняются.
#: У алгоритмов и именованных запросов обработчик один, и он весь их текст.
HANDLER_NAMES: dict[str, tuple[str, ...]] = {
    "ПВД": _PVD_HANDLERS,
    "ПКО": _PKO_HANDLERS,
    "ПРО": _PRO_HANDLERS,
    "Алгоритм": ("Текст",),
    "Запрос": ("Текст",),
}

_PAGE_CAP = 2000  # потолок страницы, чтобы ответ не раздулся до неотправляемого


def page_lines(text: str, start_line: int = 1, max_lines: int = 500) -> dict:
    """Страница текста с номерами строк и честным признаком полноты.

    Нумерация от 1 по `text.splitlines()` — тот же счёт, что у поиска, поэтому
    номер из выдачи поиска годится сюда без пересчёта. Молча обрезать нельзя:
    если показано не всё, в ответе есть «следующая строка».

    Тексты обработчиков приходят уже обрезанными по краям, так что завершающий
    перевод строки в них не встречается и при сборке страниц не теряется.
    """
    lines = text.splitlines()
    total = len(lines)
    max_lines = max(1, min(max_lines, _PAGE_CAP))
    if total == 0:
        return {
            "всего строк": 0,
            "с строки": 0,
            "по строку": 0,
            "показано строк": 0,
            "обрезано": False,
            "текст": "",
        }
    start_line = max(1, start_line)
    if start_line > total:
        raise ValueError(f"в тексте {total} строк, строки {start_line} в нём нет")

    end = min(start_line + max_lines - 1, total)
    shown = lines[start_line - 1 : end]
    page = {
        "всего строк": total,
        "с строки": start_line,
        "по строку": end,
        "показано строк": len(shown),
        "обрезано": end < total,
        "текст": "\n".join(f"{n:5d} | {ln}" for n, ln in enumerate(shown, start_line)),
    }
    if page["обрезано"]:
        page["следующая строка"] = end + 1
    return page


def _rule_label(kind: str, rule) -> str:
    """Как назвать правило в сообщении об ошибке: один код мало что объясняет."""
    parts = [f"{kind} {rule.code}"]
    if rule.name:
        parts.append(f"«{rule.name}»")
    obj = obj_short(getattr(rule, "obj", ""))
    if obj:
        parts.append(f"объект {obj}")
    return ", ".join(parts)


def handler_names(kind: str, handlers: dict[str, str]) -> list[str]:
    """Имена обработчиков в порядке выполнения, а не как легли в словарь."""
    order = HANDLER_NAMES.get(kind, ())
    return [n for n in order if n in handlers] + [n for n in handlers if n not in order]


def handler_text(rule, kind: str, name: str) -> tuple[str, str]:
    """Обработчик по имени без учёта регистра: (настоящее имя, текст)."""
    for real, text in rule.handlers.items():
        if real.casefold() == name.casefold():
            return real, text
    have = ", ".join(handler_names(kind, rule.handlers)) or "ни одного"
    raise LookupError(f"у правила «{rule.code}» нет обработчика «{name}». Есть: {have}")


def matching_lines(code: str, needle: str, limit: int = 5) -> list[tuple[int, str, bool]]:
    """Строки с совпадением: номер, текст (до 200 знаков), признак обрезки.

    Закомментированные строки пропускаются: закомментированный вызов правила
    активным не считается.
    """
    out: list[tuple[int, str, bool]] = []
    for number, line in enumerate(code.splitlines(), 1):
        stripped = line.strip()
        if needle in stripped.casefold() and not stripped.startswith("//"):
            out.append((number, stripped[:200], len(stripped) > 200))
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Обработчики реквизитов (ПКС)

#: У реквизита обработчик один — это и есть «условие заполнения реквизита».
PROP_HANDLER = "ПередВыгрузкой"


def all_props(pko: Pko) -> list[tuple[str, Prop]]:
    """(табличная часть или "", реквизит): сначала шапка, потом табличные части."""
    return [("", p) for p in pko.props] + [
        (s.dst or s.name, p) for s in pko.sections for p in s.props
    ]


def prop_target(prop: Prop) -> str:
    """Куда уходит значение: реквизит приёмника или параметр для подчинённого правила.

    У каждого четвёртого реквизита с обработчиком приёмника нет — значение
    передаётся параметром, и без этого поле выглядело бы пустым.
    """
    if prop.dst:
        return prop.dst
    return f"параметр {prop.param}" if prop.param else "без приёмника"


def _prop_label(section: str, prop: Prop) -> str:
    parts = [f"код {prop.code}", f"приёмник {prop_target(prop)}"]
    if section:
        parts.append(f"табличная часть {section}")
    parts.append("есть обработчик" if prop.handler else "без обработчика")
    return ", ".join(parts)


def find_prop(pko: Pko, name: str) -> tuple[str, Prop]:
    """Реквизит правила конвертации: сперва по коду, потом по имени приёмника.

    Код всегда заполнен и уникален внутри правила. Имя приёмника удобнее, но
    внутри одного правила повторяется (шапка и табличная часть), поэтому
    многозначность — ошибка с перечислением, а не выбор наугад.
    Имя параметра для передачи считается именем приёмника: другого у таких нет.
    """
    rows = all_props(pko)
    needle = name.strip().casefold()
    for what, match in (
        ("код", lambda p: p.code.casefold() == needle),
        ("имя приёмника", lambda p: needle in (p.dst.casefold(), p.param.casefold())),
    ):
        found = [(s, p) for s, p in rows if needle and match(p)]
        if len(found) == 1:
            return found[0]
        if found:
            listed = "; ".join(_prop_label(s, p) for s, p in found)
            raise LookupError(
                f"«{name}» — {what} сразу нескольких реквизитов правила {pko.code}: {listed}."
                " Уточните код реквизита."
            )
    near = sorted({p.dst or p.param for _, p in rows if needle in (p.dst or p.param).casefold()})
    raise LookupError(
        f"у правила конвертации {pko.code} нет реквизита «{name}»."
        + (f" Похожие: {', '.join(near[:10])}" if near else "")
    )


def prop_handler(pko: Pko, name: str, handler: str = "") -> tuple[str, Prop]:
    """Реквизит, у которого есть обработчик: (табличная часть, реквизит).

    handler можно не указывать; указан — обязан быть ПередВыгрузкой, других у
    реквизита не бывает, и молча подменять чужое имя нельзя.
    """
    if handler and handler.casefold() != PROP_HANDLER.casefold():
        raise LookupError(f"у реквизита один обработчик — {PROP_HANDLER}, а не «{handler}»")
    section, prop = find_prop(pko, name)
    if not prop.handler:
        raise LookupError(
            f"у реквизита {prop_target(prop)} (код {prop.code}) правила {pko.code} обработчика нет:"
            " значение переносится прямым сопоставлением"
        )
    return section, prop


# ---------------------------------------------------------------------------
# Сборка индекса


def _parse_snippets(node: ET.Element | None, tag: str, group: str = "") -> dict[str, Snippet]:
    """Алгоритмы и именованные запросы. Имя лежит в атрибуте, текст — в теге."""
    out: dict[str, Snippet] = {}
    for child in node if node is not None else ():
        if child.tag == "Группа":
            out.update(_parse_snippets(child, tag, child.get("Имя", "")))
        elif child.tag == tag:
            name = child.get("Имя", "")
            text = _text(child, "Текст")
            if not name:
                continue
            out[name] = Snippet(
                code=name,
                name=name,
                disabled=_disabled(child),
                handlers={"Текст": text} if text else {},
                group=group,
                pko_calls=_pko_calls(text),
            )
    return out


def load_direction(folder: Path) -> Direction | None:
    """Читает одну папку направления. Возвращает None, если правил в ней нет."""
    exchange = folder / "ExchangeRules.xml"
    if not exchange.exists():
        return None

    raw = exchange.read_text(encoding="utf-8")
    root = ET.fromstring(raw)
    src_el, dst_el = root.find("Источник"), root.find("Приемник")
    direction = Direction(
        key=folder.name,
        path=folder,
        src_config=(src_el.get("СинонимКонфигурации", "") if src_el is not None else ""),
        dst_config=(dst_el.get("СинонимКонфигурации", "") if dst_el is not None else ""),
        raw=raw,
    )

    for el, off in _walk_rules(root.find("ПравилаКонвертацииОбъектов")):
        pko = _parse_pko(el, off)
        if pko.code:
            direction.pko[pko.code] = pko
    for el, off in _walk_rules(root.find("ПравилаВыгрузкиДанных")):
        direction.pvd.append(_parse_pvd(el, off))
    direction.algorithms = _parse_snippets(root.find("Алгоритмы"), "Алгоритм")
    direction.queries = _parse_snippets(root.find("Запросы"), "Запрос")

    registration = folder / "RegistrationRules.xml"
    if registration.exists():
        direction.raw_reg = registration.read_text(encoding="utf-8")
        reg_root = ET.fromstring(direction.raw_reg)
        for el, off in _walk_rules(reg_root.find("ПравилаРегистрацииОбъектов")):
            direction.pro.append(_parse_pro(el, off))

    return direction


class Index:
    """Индекс всех направлений. Строится целиком при старте, живёт в памяти."""

    def __init__(self, root: Path):
        self.root = root
        self.directions: dict[str, Direction] = {}
        self.reload()

    def reload(self) -> int:
        found: dict[str, Direction] = {}
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            direction = load_direction(folder)
            if direction is not None:
                found[direction.key] = direction
        self.directions = found
        return len(found)

    # -- разрешение имени направления ------------------------------------

    def sides(self, direction: Direction) -> list[str]:
        """Стороны направления по порядку: [источник, приёмник].

        Берём из самих правил, а имя папки — запасной вариант.
        """
        from_rules = [
            _norm(short_config(direction.src_config)),
            _norm(short_config(direction.dst_config)),
        ]
        if all(from_rules):
            return from_rules
        return _side_tokens(direction.key)

    def resolve(self, name: str) -> Direction:
        """Находит направление по имени папки или человеческому названию.

        Понимает «УПП_ERP», «упп erp», «УПП → ЕРП», «из розницы в ерп».
        Порядок сторон значим: УПП → ЕРП и ЕРП → УПП это разные направления.
        """
        if not self.directions:
            raise LookupError("индекс пуст: правила не найдены")
        if name in self.directions:
            return self.directions[name]

        wanted = _side_tokens(name)
        if not wanted:
            raise LookupError(_choices(self.directions))

        exact: list[Direction] = []
        loose: list[Direction] = []
        for direction in self.directions.values():
            for variant in (self.sides(direction), _side_tokens(direction.key)):
                if len(wanted) == len(variant) and all(
                    _same(a, b) for a, b in zip(wanted, variant)
                ):
                    exact.append(direction)
                    break
            else:
                if all(any(_same(t, s) for s in self.sides(direction)) for t in wanted):
                    loose.append(direction)

        picked = exact or loose
        if not picked:
            raise LookupError(_choices(self.directions))
        if len(picked) > 1:
            tied = ", ".join(f"{d.key} ({d.title})" for d in picked)
            raise LookupError(f"направление понято неоднозначно, подходят: {tied}")
        return picked[0]

    # -- выборки ----------------------------------------------------------

    def pvd_blocks(self, direction: Direction, pvd: Pvd) -> list[str]:
        """Функциональные блоки ветки: свои плюс метки вызываемых из неё ПКО."""
        found = set(pvd.blocks)
        for name in pvd.pko_calls:
            pko = direction.pko.get(name)
            if pko is not None and not pko.disabled:
                found.update(pko.blocks)
        return sorted(found)

    def pvd_block_sources(self, direction: Direction, pvd: Pvd) -> dict:
        """Откуда у ветки метки процессов: свои или от вызываемых ПКО, и от каких.

        pvd_blocks сливает оба источника, и метка чужого правила выглядит как
        действующая выгрузка ветки, даже если своя ветка по ней закомментирована.
        Объединение «своих» и «пришедших» равно pvd_blocks.
        """
        inherited: dict[str, list[str]] = {}
        for name in pvd.pko_calls:
            pko = direction.pko.get(name)
            if pko is not None and not pko.disabled:
                for block in pko.blocks:
                    inherited.setdefault(block, []).append(name)
        return {
            "свои": sorted(pvd.blocks),
            "от вызываемых правил": {b: sorted(n) for b, n in sorted(inherited.items())},
        }

    def documents(self, direction: Direction) -> dict[str, dict]:
        """Объекты, по которым в направлении вообще есть правила."""
        docs: dict[str, dict] = {}

        def entry(name: str) -> dict:
            return docs.setdefault(name, {"про": 0, "пвд": 0, "блоки": set()})

        for pro in direction.pro:
            if not pro.disabled:
                entry(obj_short(pro.obj))["про"] += 1
            else:
                entry(obj_short(pro.obj))
        for pvd in direction.pvd:
            item = entry(obj_short(pvd.obj))
            if not pvd.disabled:
                item["пвд"] += 1
                item["блоки"].update(self.pvd_blocks(direction, pvd))
        return docs

    def blocks(self, direction: Direction) -> dict[str, list[str]]:
        """Функциональный блок → документы, ветки которых им помечены."""
        out: dict[str, set[str]] = {}
        for pvd in direction.pvd:
            if pvd.disabled:
                continue
            for block in self.pvd_blocks(direction, pvd):
                out.setdefault(block, set()).add(obj_short(pvd.obj))
        return {block: sorted(docs) for block, docs in sorted(out.items())}

    def pvd_for(self, direction: Direction, document: str) -> list[Pvd]:
        needle = document.casefold()
        return [p for p in direction.pvd if obj_short(p.obj).casefold() == needle]

    def pro_for(self, direction: Direction, document: str) -> list[Pro]:
        needle = document.casefold()
        return [p for p in direction.pro if obj_short(p.obj).casefold() == needle]

    def find_pko(self, direction: Direction, name: str) -> Pko | None:
        if name in direction.pko:
            return direction.pko[name]
        needle = name.casefold()
        for pko in direction.pko.values():
            if pko.code.casefold() == needle or pko.name.casefold() == needle:
                return pko
        return None

    @staticmethod
    def _check_kind(kind: str) -> None:
        """Опечатка в виде правила должна быть ошибкой, а не пустой выдачей."""
        if kind and kind not in RULE_KINDS:
            raise LookupError(f"вид правила «{kind}» неизвестен. Бывают: {', '.join(RULE_KINDS)}")

    def _pools(self, direction: Direction, kind: str = "") -> dict[str, list]:
        """Правила направления по видам. Пустой вид — все три."""
        self._check_kind(kind)
        pools: dict[str, list] = {
            "ПВД": list(direction.pvd),
            "ПКО": list(direction.pko.values()),
            "ПРО": list(direction.pro),
            "Алгоритм": list(direction.algorithms.values()),
            "Запрос": list(direction.queries.values()),
        }
        return pools if not kind else {kind: pools[kind]}

    def find_rule(self, direction: Direction, code: str, kind: str = "") -> tuple[str, object]:
        """Правило по коду, наименованию или объекту: (вид правила, само правило).

        У правил регистрации коды числовые («000000072») — назвать такое правило
        человек не может, поэтому в ход идут ещё наименование и имя объекта.
        Подошло несколько — просим уточнить и перечисляем, а не выбираем сами.
        """
        pools = self._pools(direction, kind)
        what, hint, found = self._matching_rules(pools, code.casefold())
        if len(found) == 1:
            return found[0]
        if found:
            listed = "; ".join(_rule_label(k, r) for k, r in found)
            raise LookupError(f"«{code}» — это {what} сразу нескольких правил: {listed}. {hint}")
        near = self._near(pools, code.casefold())
        raise LookupError(
            f"правила «{code}» в направлении {direction.title} нет."
            + (f" Похожие: {', '.join(near[:10])}" if near else "")
        )

    @staticmethod
    def _matching_rules(pools: dict[str, list], needle: str) -> tuple[str, str, list]:
        """Правила по первому сработавшему признаку: (признак, подсказка, [(вид, правило)]).

        Признаки по старшинству: код, наименование, короткое имя объекта.
        Ничего не нашлось — ("", "", []).
        """
        for what, hint, match in (
            # Совпал код — уточнять его бессмысленно, он у всех один: нужен вид.
            ("код", f"Уточните вид правила: {', '.join(RULE_KINDS)}.",
             lambda r: r.code.casefold() == needle),
            ("наименование", "Уточните код нужного.",
             lambda r: r.name.casefold() == needle),
            ("объект", "Уточните код нужного.",
             lambda r: obj_short(getattr(r, "obj", "")).casefold() == needle),
        ):
            found = [(kind, rule) for kind, rules in pools.items() for rule in rules if match(rule)]
            if found:
                return what, hint, found
        return "", "", []

    @staticmethod
    def _near(pools: dict[str, list], needle: str) -> list[str]:
        """Коды и наименования, содержащие искомое, — для текста ошибки."""
        return sorted(
            {r.code for rules in pools.values() for r in rules if needle in r.code.casefold()}
            | {r.name for rules in pools.values() for r in rules if needle in r.name.casefold()}
        )

    def search(
        self,
        targets: list[Direction],
        query: str,
        kind: str = "",
        rule: str = "",
        handler: str = "",
        limit: int = 40,
        lines_per_handler: int = 5,
    ) -> list[dict]:
        """Где упоминается реквизит, алгоритм или фрагмент логики.

        Ищет по соответствиям реквизитов, отборам регистрации, текстам
        обработчиков всех видов правил и обработчикам реквизитов правил
        конвертации. Фильтры складываются по «и». Каждое попадание в коде несёт
        номер строки, годный для page_lines.

        Попадание в обработчике реквизита несёт ещё «реквизит» и «код
        реквизита»; фильтр handler для них — ПередВыгрузкой. Отдельного фильтра
        по реквизиту нет: такой обработчик не длиннее 66 строк, его проще прочитать.

        Отбор rule понимает то же, что find_rule: код, наименование, имя
        объекта — и разрешается в каждом направлении отдельно. Подошло несколько
        правил — ищем по всем: это отбор, а не адресация одного правила.
        Не нашлось ни в одном направлении — LookupError с похожими, как у
        опечатки в виде правила.
        """
        self._check_kind(kind)
        needle = query.casefold()
        wanted_handler = handler.casefold()
        hits: list[dict] = []

        allowed: dict[str, set[tuple[str, str]]] = {}
        if rule:
            near: set[str] = set()
            for exch in targets:
                pools = self._pools(exch, kind)
                found = self._matching_rules(pools, rule.casefold())[2]
                allowed[exch.key] = {(kind_, r.code) for kind_, r in found}
                if not found:
                    near.update(self._near(pools, rule.casefold()))
            if not any(allowed.values()):
                raise LookupError(
                    f"правила «{rule}» нет ни в одном направлении поиска."
                    + (f" Похожие: {', '.join(sorted(near)[:10])}" if near else "")
                )

        def suits(exch: Direction, kind_: str, code: str) -> bool:
            if kind and kind != kind_:
                return False
            return not rule or (kind_, code) in allowed[exch.key]

        def suits_handler(name: str) -> bool:
            return not wanted_handler or name.casefold() == wanted_handler

        def add(exch: Direction, kind_: str, code: str, where: str, **rest) -> bool:
            hits.append(
                {"направление": exch.key, "вид правила": kind_, "правило": code, "где": where, **rest}
            )
            return len(hits) >= limit

        def add_code(
            exch: Direction, kind_: str, code: str, where: str, name: str, text: str, **extra
        ) -> bool:
            for number, fragment, cut in matching_lines(text, needle, lines_per_handler):
                item = {**extra, "обработчик": name, "строка": number, "фрагмент": fragment}
                if cut:
                    item["фрагмент обрезан"] = True
                if add(exch, kind_, code, where, **item):
                    return True
            return False

        for exch in targets:
            for pko in exch.pko.values():
                if not suits(exch, "ПКО", pko.code):
                    continue
                where = f"правило конвертации {pko.code}"
                # Соответствия реквизитов — не обработчик: при отборе по
                # обработчику они только шумят.
                if not wanted_handler:
                    props = list(pko.props) + [p for s in pko.sections for p in s.props]
                    for prop in props:
                        if needle in f"{prop.src} {prop.dst} {prop.dst_type}".casefold():
                            what = f"{prop.src or '—'} → {prop.dst} ({prop.dst_type})"
                            if add(exch, "ПКО", pko.code, where, что=what):
                                return hits
                for name, text in pko.handlers.items():
                    if suits_handler(name) and add_code(exch, "ПКО", pko.code, where, name, text):
                        return hits
                # Обработчики реквизитов: там живёт условие заполнения реквизита.
                if not suits_handler(PROP_HANDLER):
                    continue
                for section, prop in all_props(pko):
                    if not prop.handler:
                        continue
                    target = prop_target(prop)
                    extra = {"реквизит": target, "код реквизита": prop.code}
                    if section:
                        extra["табличная часть"] = section
                    shown = f"{section}.{target}" if section else target
                    if add_code(exch, "ПКО", pko.code, f"{where}, реквизит {shown}",
                                PROP_HANDLER, prop.handler, **extra):
                        return hits

            for pvd in exch.pvd:
                if not suits(exch, "ПВД", pvd.code):
                    continue
                where = f"выгрузка {obj_short(pvd.obj)}"
                for name, text in pvd.handlers.items():
                    if suits_handler(name) and add_code(exch, "ПВД", pvd.code, where, name, text):
                        return hits

            for pro in exch.pro:
                if not suits(exch, "ПРО", pro.code):
                    continue
                where = f"регистрация {obj_short(pro.obj)}"
                if not wanted_handler and needle in pro.obj_filter.casefold():
                    if add(exch, "ПРО", pro.code, where, что="отбор по свойствам объекта"):
                        return hits
                for name, text in pro.handlers.items():
                    if suits_handler(name) and add_code(exch, "ПРО", pro.code, where, name, text):
                        return hits

            for kind_, pool, label in (
                ("Алгоритм", exch.algorithms, "общий алгоритм"),
                ("Запрос", exch.queries, "именованный запрос"),
            ):
                for snip in pool.values():
                    if not suits(exch, kind_, snip.code):
                        continue
                    where = f"{label} {snip.code}"
                    for name, text in snip.handlers.items():
                        if suits_handler(name) and add_code(
                            exch, kind_, snip.code, where, name, text
                        ):
                            return hits

        return hits

    def algorithms_used(
        self, direction: Direction, code: str, seen: set[str] | None = None
    ) -> dict[str, list[str]]:
        """Общие алгоритмы, вызываемые из текста, и спрятанные в них вызовы ПКО.

        Ветка выгрузки зовёт алгоритм через `Выполнить(Алгоритмы.Имя)`, а внутри
        алгоритма может стоять ВыгрузитьПоПравилу. В самой ветке такого вызова не
        видно, поэтому без этого обхода часть документов приёмника теряется.
        Алгоритмы зовут друг друга, поэтому идём вглубь.
        """
        seen = seen if seen is not None else set()
        out: dict[str, list[str]] = {}
        for name in sorted(set(_ALG_CALL_RE.findall(_uncommented(code)))):
            snippet = direction.algorithms.get(name)
            if snippet is None or name in seen:
                continue
            seen.add(name)
            if snippet.pko_calls:
                out[name] = snippet.pko_calls
            out.update(self.algorithms_used(direction, "\n".join(snippet.handlers.values()), seen))
        return out

    def algorithm_call_places(self, direction: Direction, rule, kind: str) -> list[dict]:
        """Вызовы ПКО из общих алгоритмов с адресами — два звена цепочки.

        Первое — где правило зовёт алгоритм (обработчик и строки), последнее —
        где в алгоритме стоит вызов ПКО. Промежуточные алгоритмы не
        показываются: путь целиком строить незачем, оба конца читаются напрямую.
        """
        out: list[dict] = []
        for name in handler_names(kind, rule.handlers):
            for first, lines in _lines_of(rule.handlers[name], (_ALG_CALL_RE,)).items():
                snippet = direction.algorithms.get(first)
                if snippet is None:
                    continue
                text = "\n".join(snippet.handlers.values())
                reached = {first: snippet.pko_calls} if snippet.pko_calls else {}
                reached.update(self.algorithms_used(direction, text, {first}))
                for last, calls in reached.items():
                    inner = pko_call_lines("\n".join(direction.algorithms[last].handlers.values()))
                    for pko in calls:
                        out.append(
                            {
                                "правило конвертации": pko,
                                "вызов в алгоритме": {"алгоритм": last, "строки": inner.get(pko, [])},
                                "правило зовёт алгоритм": {
                                    "алгоритм": first,
                                    "обработчик": name,
                                    "строки": lines,
                                },
                            }
                        )
        return out

    def carried_by(self, direction: Direction, obj: str) -> list[dict]:
        """Правила конвертации, переносящие объект, у которого нет своей выгрузки.

        Так живут справочники: собственной ветки выгрузки у них нет, их тянут
        подчинённые правила из документов. Заодно показываем, из каких веток.
        """
        needle = obj.casefold()
        out: list[dict] = []
        for pko in direction.pko.values():
            if needle not in (obj_short(pko.src).casefold(), obj_short(pko.dst).casefold()):
                continue
            reachable = sorted(
                {
                    obj_short(pvd.obj)
                    for pvd in direction.pvd
                    if not pvd.disabled
                    and (
                        pko.code in pvd.pko_calls
                        or any(pko.code in self.cascades(direction, c) for c in pvd.pko_calls)
                    )
                }
            )
            out.append(
                {
                    "правило": pko.code,
                    "отключено": pko.disabled,
                    "перенос": f"{pko.src} → {pko.dst}",
                    "вызывается при выгрузке": reachable or ["не достигается ни одной веткой"],
                }
            )
        return sorted(out, key=lambda item: item["правило"])

    def cascades(self, direction: Direction, code: str, seen: set[str] | None = None) -> list[str]:
        """Подчинённые ПКО, вызываемые из ПКС, вглубь. Это скрытые документы приёмника."""
        seen = seen or set()
        pko = direction.pko.get(code)
        if pko is None or code in seen:
            return []
        seen.add(code)
        out: list[str] = []
        props = list(pko.props) + [p for s in pko.sections for p in s.props]
        for prop in props:
            child = prop.conv_rule
            if not child or prop.disabled or child in seen:
                continue
            out.append(child)
            out.extend(self.cascades(direction, child, seen))
        return out


_STOP = {"из", "в", "во", "на", "обмен", "обмена", "направление", "правила"}


def _norm(word: str) -> str:
    return _ALIAS.get(word.casefold(), word.casefold())


def _side_tokens(text: str) -> list[str]:
    """Стороны направления в порядке упоминания: «из розницы в ерп» → [розницы, ерп]."""
    out: list[str] = []
    for part in re.split(r"[^0-9A-Za-zА-Яа-я]+", text.casefold()):
        if not part or part in _STOP:
            continue
        token = _norm(part)
        if not out or out[-1] != token:
            out.append(token)
    return out


def _same(a: str, b: str) -> bool:
    """Мягкое сравнение сторон: «розницы» и «розница» — одно и то же."""
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    return len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5]


_ALIAS = {
    "erp": "ерп",
    "erp2": "ерп",
    "ерп2": "ерп",
    "upp": "упп",
    "retail": "розница",
    "roznica": "розница",
    "itilium": "итилиум",
    "иитилум": "итилиум",
}


def _choices(directions: dict[str, Direction]) -> str:
    listed = ", ".join(f"{d.key} ({d.title})" for d in directions.values())
    return f"направление не распознано. Доступны: {listed}"
