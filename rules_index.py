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
    handlers = _handlers(
        el,
        ("ПередВыгрузкой", "ПриВыгрузке", "ПослеВыгрузки", "ПриЗагрузке", "ПослеЗагрузки"),
    )
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


def _parse_pvd(el: ET.Element, off: bool) -> Pvd:
    handlers = _handlers(el, _PVD_HANDLERS)
    joined = "\n".join(handlers.values())
    active_code = _uncommented(joined)
    calls = sorted(set(_IMYA_PKO_RE.findall(active_code)) | set(_VYGR_RE.findall(active_code)))
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
    )


# ---------------------------------------------------------------------------
# Разбор ПРО


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
        handlers=_handlers(
            el, ("ПередОбработкой", "ПриОбработке", "ПриОбработкеДополнительный", "ПослеОбработки")
        ),
        vidops=sorted(set(_VIDOP_RE.findall(ET.tostring(el, encoding="unicode")))),
    )


# ---------------------------------------------------------------------------
# Сборка индекса


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
