"""Проверки правил обмена без конфигураций: что в правилах сломано или не сработает.

Перенесены из kd2-rules-mcp (https://github.com/egordoronchenko/kd2-rules-mcp, MIT) —
те проверки его rules_validate, которым не нужны структуры конфигураций. Обоснования
строками кода БСП — там, в docs/checks.md; утверждения о ветках выгрузки сверены ещё и
с БСП 2.3. Добавлены две свои: вторая ветка на тот же объект и «Отказ» в обработчике
«перед обработкой правила», который при обмене через план ничего не отменяет.

Каждое замечание несёт адрес для чтения: правило, обработчик, строку, код реквизита
или табличной части — с ними get_rule_handler открывает нужное место.
Только stdlib, от сервера не зависит.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from rules_index import (
    CONVERSION_CODE,
    Direction,
    Pko,
    _IMYA_PKO_RE,
    _VYGR_RE,
    all_props,
    obj_short,
    prop_target,
)

ERROR = "ошибка"
WARNING = "предупреждение"

#: Проверка → (уровень, чем грозит). Порядок — порядок выдачи.
CHECKS: dict[str, tuple[str, str]] = {
    "нет алгоритма": (
        ERROR,
        "обработчик падает на этой строке: ошибка в протоколе обмена; если не включено"
        " «продолжать при ошибке», выгрузка (загрузка) прерывается целиком",
    ),
    "алгоритм другого режима": (
        ERROR,
        "в момент выполнения обработчика такого алгоритма нет — та же ошибка, что и при"
        " отсутствующем алгоритме",
    ),
    "нет правила конвертации": (
        ERROR,
        "БСП пишет в протокол, что правила нет; объект или значение не выгружается",
    ),
    "повтор кода": (
        ERROR,
        "БСП хранит такие правила по коду или имени: последнее молча затирает первое",
    ),
    "вторая ветка на тот же объект": (
        WARNING,
        "ветка не выполняется: для объекта из очереди БСП берёт первую включённую ветку"
        " его типа",
    ),
    "в ветке не действует": (
        WARNING,
        "при обмене через план обмена эта часть ветки не исполняется; при ручном обмене"
        " универсальной обработкой — исполняется",
    ),
    "объект не регистрируется": (
        WARNING,
        "при обмене через план ветке нечего выгружать: разбирать её как действующую"
        " выгрузку нельзя. Состав плана взят из снимка в правилах регистрации — окончательно"
        " проверяется по конфигурации источника",
    ),
    "правило не вызывается": (
        WARNING,
        "правило мёртвое, если его не подбирает БСП по типу значения (реквизит без"
        " указанного правила): разбирать его поведение незачем, пока не доказан вызов",
    ),
    "только ссылкой": (
        WARNING,
        "в приёмник уходят только поля поиска; остальные реквизиты этого правила не"
        " переносятся, правка их соответствий ничего не меняет",
    ),
    "поиск: нет ключей": (
        WARNING,
        "приёмнику нечем искать: при каждой загрузке создаётся новый объект — дубли",
    ),
    "поиск: ключ только ЭтоГруппа": (
        WARNING,
        "разные объекты находят один и тот же элемент приёмника и по очереди его"
        " перезаписывают",
    ),
    "поиск: обработчик не вызывается": (
        WARNING,
        "обработчик поиска мёртвый: объект ищется только по идентификатору",
    ),
    "поиск: продолжение без полей": (
        WARNING,
        "по полям объект не находится, и при неудаче поиска по идентификатору создаётся"
        " новый — дубли",
    ),
    "поиск: имя не из полей поиска": (
        WARNING,
        "имя молча выпадает из условия поиска: ищут не по тому, что написано",
    ),
    "поиск: параметр не передаётся": (
        WARNING,
        "в обработчике поиска параметра нет, ветка по нему не выполнится",
    ),
    "поиск: параметр без признака поиска": (
        WARNING,
        "параметр приходит уже после поиска, ветка обработчика по нему не выполнится —"
        " типичная причина дублей",
    ),
    "запись объекта после загрузки": (
        WARNING,
        "объект пишется до установки режима загрузки: документ проводится дважды или"
        " загрузка прерывается",
    ),
}

# Фаза обработчика: при загрузке в структуре «Алгоритмы» только алгоритмы с признаком
# «используется при загрузке», при выгрузке — только без него (БСП: ЗагрузитьАлгоритм).
_IMPORT_EVENTS = {
    "Конвертация": {
        "ПередЗагрузкойДанных",
        "ПослеЗагрузкиДанных",
        "ПослеЗагрузкиПараметров",
        "ПередЗагрузкойОбъекта",
        "ПослеЗагрузкиОбъекта",
        "ПриПолученииИнформацииОбУдалении",
        "ПослеПолученияИнформацииОбУзлахОбмена",
    },
    "ПКО": {"ПередЗагрузкой", "ПоследовательностьПолейПоиска", "ПриЗагрузке", "ПослеЗагрузки"},
}
_EXPORT, _IMPORT = "выгрузке", "загрузке"

# Ссылочные приёмники, для которых БСП создаёт новый объект (СоздатьНовыйОбъект):
# перечисление и точка маршрута не создаются.
_REF_CREATING = (
    "СправочникСсылка.",
    "ДокументСсылка.",
    "ПланВидовХарактеристикСсылка.",
    "ПланСчетовСсылка.",
    "ПланВидовРасчетаСсылка.",
    "ПланОбменаСсылка.",
    "БизнесПроцессСсылка.",
    "ЗадачаСсылка.",
)
_SYSTEM_SEARCH_NAMES = {"{уникальныйидентификатор}", "{имяпредопределенногоэлемента}"}

_STRING = re.compile(r'"(?:[^"]|"")*"?')
_ALGORITHM_REF = re.compile(r"(?<![\w.])Алгоритмы\s*\.\s*([^\W\d]\w*)\b(?!\s*\()", re.IGNORECASE)
_OBJECT_WRITE = re.compile(r"(?<![\w.])Объект\s*\.\s*Записать\s*\(", re.IGNORECASE)
_SELECTION = re.compile(r"(?<![\w.])ВыборкаДанных\s*=", re.IGNORECASE)
_REFUSAL = re.compile(r"(?<![\w.])Отказ\s*=\s*Истина\b", re.IGNORECASE)
_EXPORT_WHOLE = re.compile(r"(?<![\w.])ВыгрузитьОбъект\s*=\s*Истина\b", re.IGNORECASE)
_PARAMETER = re.compile(
    r'ПараметрыОбъекта\s*(?:\[\s*"([^"\n]+)"\s*\]|\.\s*Получить\s*\(\s*"([^"\n]+)"\s*\))',
    re.IGNORECASE,
)
_SEARCH_STRING = re.compile(
    r"(?:^|;|\bТогда|\bИначе|\bЦикл)\s*СтрокаИменСвойствПоиска\s*=\s*([^;\n]*)",
    re.IGNORECASE | re.MULTILINE,
)
_SEARCH_WRITE = re.compile(
    r'СвойстваПоиска\s*(?:\[\s*"([^"\n]+)"\s*\]\s*=|\.\s*Вставить\s*\(\s*"([^"\n]+)")',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Текст кода


def _without_comment(line: str) -> str:
    """Строка до комментария `//` вне строкового литерала.

    Строка, начинающаяся с «|», — продолжение многострочного литерала: она
    начинается внутри строки, и «//» в ней — текст запроса, а не комментарий.
    """
    quoted = line.lstrip().startswith("|")
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif not quoted and line.startswith("//", index):
            return line[:index]
    return line


def _code(text: str) -> list[str]:
    """Строки кода без комментариев; номера строк — индекс + 1, как у page_lines."""
    return [_without_comment(line) for line in text.splitlines()]


def _masked(line: str) -> str:
    """Литералы заменены пустыми кавычками: имя внутри строки — не обращение."""
    if line.lstrip().startswith("|"):
        return ""
    return _STRING.sub('""', line)


def _literals(text: str) -> set[str]:
    """Строковые литералы активного кода, без кавычек и пробелов по краям."""
    out: set[str] = set()
    for line in _code(text):
        for match in _STRING.finditer(line):
            out.add(match.group(0).strip('"').replace('""', '"').strip())
    return out


def _expand(text: str, d: Direction, seen: set[str] | None = None) -> str:
    """Текст вместе с текстами алгоритмов, которые он вызывает, вглубь.

    `Выполнить(Алгоритмы.Имя)` исполняет алгоритм в контексте обработчика: заданная
    в алгоритме переменная — переменная обработчика.
    """
    seen = seen if seen is not None else set()
    parts = [text]
    by_key = {name.casefold(): s for name, s in d.algorithms.items()}
    for line in _code(text):
        for name in _ALGORITHM_REF.findall(_masked(line)):
            snippet = by_key.get(name.casefold())
            if snippet is None or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            parts.append(_expand(snippet.handlers.get("Текст", ""), d, seen))
    return "\n".join(parts)


def _texts(d: Direction) -> Iterator[tuple[str, str, dict, str, str, str]]:
    """Все исполняемые тексты правил обмена: (вид, код, адрес, обработчик, текст, место).

    Место — для фазы обработчика: ПКО, ПКС, ПВД, Конвертация, Алгоритм.
    Отключённые ветки, реквизиты и табличные части не исполняются и не смотрятся.
    Правила регистрации — другой исполнитель, алгоритмов правил обмена там нет.
    """
    for pko in d.pko.values():
        for name, text in pko.handlers.items():
            yield "ПКО", pko.code, {}, name, text, "ПКО"
        for section in pko.sections:
            if section.disabled:
                continue
            place = {
                "табличная часть": section.dst or section.name,
                "код табличной части": section.code,
            }
            for name, text in section.handlers.items():
                yield "ПКО", pko.code, place, name, text, "ПКС"
        for section_name, prop in all_props(pko):
            if prop.disabled:
                continue
            place = {"реквизит": prop_target(prop), "код реквизита": prop.code}
            if section_name:
                place["табличная часть"] = section_name
            for name, text in prop.handlers.items():
                yield "ПКО", pko.code, place, name, text, "ПКС"
    for pvd in d.pvd:
        if not pvd.disabled:
            for name, text in pvd.handlers.items():
                yield "ПВД", pvd.code, {}, name, text, "ПВД"
    if d.conversion is not None:
        for name, text in d.conversion.handlers.items():
            yield "Конвертация", CONVERSION_CODE, {}, name, text, "Конвертация"
    for snippet in d.algorithms.values():
        for name, text in snippet.handlers.items():
            yield "Алгоритм", snippet.code, {}, name, text, "Алгоритм"


def _finding(check: str, kind: str, code: str, message: str, **where) -> dict:
    level, threat = CHECKS[check]
    return {
        "проверка": check,
        "уровень": level,
        "вид правила": kind,
        "правило": code,
        **where,
        "суть": message,
        "чем грозит": threat,
    }


# ---------------------------------------------------------------------------
# Проверки


def _algorithms(d: Direction) -> Iterator[dict]:
    """Обращения `Алгоритмы.Имя` к алгоритму, которого нет, или нет в этой фазе."""
    phases = {
        phase: {name.casefold() for name, s in d.algorithms.items() if s.on_import == (phase == _IMPORT)}
        for phase in (_EXPORT, _IMPORT)
    }
    for kind, code, place, handler, text, where in _texts(d):
        if where == "Алгоритм":
            phase = _IMPORT if d.algorithms[code].on_import else _EXPORT
        elif kind == "Конвертация" and handler == "ПослеЗагрузкиПравилОбмена":
            phase = None  # вызывается после чтения правил в обоих режимах
        else:
            phase = _IMPORT if handler in _IMPORT_EVENTS.get(where, ()) else _EXPORT
        for number, line in enumerate(_code(text), 1):
            for name in dict.fromkeys(_ALGORITHM_REF.findall(_masked(line))):
                key = name.casefold()
                at = {**place, "обработчик": handler, "строка": number}
                if phase is not None and key in phases[phase]:
                    continue
                other = [p for p in (_EXPORT, _IMPORT) if key in phases[p]]
                if not other:
                    yield _finding(
                        "нет алгоритма", kind, code,
                        f"обращение к алгоритму «{name}», а такого алгоритма в правилах нет",
                        **at,
                    )
                elif phase is not None:
                    yield _finding(
                        "алгоритм другого режима", kind, code,
                        f"обработчик выполняется при {phase}, а алгоритм «{name}» есть только"
                        f" при {other[0]} (признак «используется при загрузке»)",
                        **at,
                    )


def _dangling(d: Direction) -> Iterator[dict]:
    """Ссылки на ПКО, которого нет: в свойствах ветки и реквизита, вызовы в коде."""
    for pvd in d.pvd:
        if not pvd.disabled and pvd.conv_rule and pvd.conv_rule not in d.pko:
            yield _finding(
                "нет правила конвертации", "ПВД", pvd.code,
                f"в свойствах ветки указано правило «{pvd.conv_rule}», а его нет",
            )
    for pko in d.pko.values():
        for section_name, prop in all_props(pko):
            if not prop.disabled and prop.conv_rule and prop.conv_rule not in d.pko:
                at = {"реквизит": prop_target(prop), "код реквизита": prop.code}
                if section_name:
                    at["табличная часть"] = section_name
                yield _finding(
                    "нет правила конвертации", "ПКО", pko.code,
                    f"реквизит переносится правилом «{prop.conv_rule}», а его нет", **at,
                )
    for kind, code, place, handler, text, _ in _texts(d):
        live = "\n".join(_code(text))
        for pattern in (_IMYA_PKO_RE, _VYGR_RE):
            for match in pattern.finditer(live):
                name = match.group(1).strip()
                if name and name not in d.pko:
                    yield _finding(
                        "нет правила конвертации", kind, code,
                        f"код вызывает правило «{name}», а его нет",
                        **place, обработчик=handler,
                        строка=live.count("\n", 0, match.start(1)) + 1,
                    )


def _repeats(d: Direction) -> Iterator[dict]:
    for kind, name in d.duplicates:
        what = "правил конвертации с кодом" if kind == "ПКО" else "алгоритмов с именем"
        yield _finding("повтор кода", kind, name, f"в правилах несколько {what} «{name}»")


def _branches(d: Direction) -> Iterator[dict]:
    """Ветки выгрузки: вторая на тот же объект и части, которые обмен через план не исполняет."""
    for pvd in d.pvd:
        if pvd.disabled:
            continue
        if pvd.shadowed_by:
            yield _finding(
                "вторая ветка на тот же объект", "ПВД", pvd.code,
                f"для объекта {obj_short(pvd.obj)} раньше в правилах стоит включённая ветка"
                f" «{pvd.shadowed_by}» — работает она",
            )
        # При обмене через план выгружается только очередь узла. Без правил регистрации
        # туда ставит лишь авторегистрация объекта из состава плана.
        elif d.plan_content is not None and not any(
            p.obj == pvd.obj and not p.disabled for p in d.pro
        ):
            if pvd.obj not in d.plan_content:
                yield _finding(
                    "объект не регистрируется", "ПВД", pvd.code,
                    f"правил регистрации на {obj_short(pvd.obj)} нет, и в составе плана его нет:"
                    " в очередь он не попадает, ветка срабатывает только при ручной выгрузке"
                    " универсальной обработкой",
                )
            elif not d.plan_content[pvd.obj]:
                yield _finding(
                    "объект не регистрируется", "ПВД", pvd.code,
                    f"правил регистрации на {obj_short(pvd.obj)} нет, в составе плана он без"
                    " авторегистрации: ветка срабатывает только на объекты, зарегистрированные"
                    " вручную",
                )
        reasons: list[str] = []
        lines: list[int] = []
        if pvd.custom_selection:
            reasons.append(
                "способ отбора «произвольный алгоритм»: ветку запускают объекты"
                f" {obj_short(pvd.obj)} из очереди на выгрузку, отбор не исполняется"
            )
        before = pvd.handlers.get("ПередОбработкойПравила", "")
        expanded = [_masked(line) for line in _code(_expand(before, d))]
        own = [_masked(line) for line in _code(before)]
        for pattern, reason in (
            (_SELECTION, "выборка данных, заданная в «перед обработкой правила», не используется"),
            (_REFUSAL, "«Отказ» в «перед обработкой правила» ничего не отменяет — код после него"
                       " выполняется, ветка работает"),
        ):
            if any(pattern.search(line) for line in expanded):
                reasons.append(reason)
                lines += [n for n, line in enumerate(own, 1) if pattern.search(line)]
        if reasons:
            at = {"обработчик": "ПередОбработкойПравила"} if lines else {}
            if lines:
                at["строки"] = sorted(set(lines))
            yield _finding("в ветке не действует", "ПВД", pvd.code, "; ".join(reasons), **at)


def _references(d: Direction) -> tuple[set[str], set[str], dict[str, list[dict]]]:
    """Кто ссылается на ПКО: (из веток, из кода вне реквизитов, {код: ссылающиеся реквизиты}).

    Вызов в коде — строковый литерал, равный коду правила: так его передают в
    ВыгрузитьПоПравилу и ИмяПКО. Код, собранный из кусков, отсюда не виден.
    В обработчике реквизита целиком выгружает только явный ВыгрузитьПоПравилу;
    другой литерал (ИмяПКО = "…") меняет правило значения реквизита, и значение
    уходит ссылкой.
    """
    from_branches = {p.conv_rule for p in d.pvd if not p.disabled and p.conv_rule}
    from_code: set[str] = set()
    from_props: dict[str, list[dict]] = {}
    for kind, code, place, handler, text, where in _texts(d):
        found = _literals(text) & d.pko.keys()
        if where == "ПВД":
            from_branches |= found
        if where == "ПКС" and "код реквизита" in place:
            whole = {n.strip() for n in _VYGR_RE.findall("\n".join(_code(text)))} & found
            from_code |= whole
            for name in found - whole:
                from_props.setdefault(name, []).append({"правило": code, **place, "целиком": False})
        else:
            from_code |= found
    for pko in d.pko.values():
        for section_name, prop in all_props(pko):
            if prop.disabled or not prop.conv_rule:
                continue
            whole = any(
                _EXPORT_WHOLE.search(_masked(line))
                for name in ("ПередВыгрузкой", "ПриВыгрузке")
                for line in _code(prop.handlers.get(name, ""))
            )
            from_props.setdefault(prop.conv_rule, []).append(
                {"правило": pko.code, "реквизит": prop_target(prop), "код реквизита": prop.code,
                 **({"табличная часть": section_name} if section_name else {}), "целиком": whole}
            )
    return from_branches, from_code, from_props


def _reach(d: Direction) -> Iterator[dict]:
    """ПКО, которое ничем не вызывается, и ПКО, которое уходит только ссылкой."""
    from_branches, from_code, from_props = _references(d)
    on_demand = {p.obj for p in d.pro if not p.disabled and p.unload_mode}
    # Для значения без указанного правила БСП берёт последнее ПКО с таким источником.
    default_for_type = {p.src: p.code for p in d.pko.values() if p.src}
    for pko in d.pko.values():
        if pko.disabled or not pko.src:  # пустой источник — выгрузка из обработчиков
            continue
        code = pko.code
        props = from_props.get(code, [])
        if code not in from_branches and code not in from_code and not props:
            note = (
                f"; но это последнее правило с источником {obj_short(pko.src)} — его БСП"
                " подставит для значения этого типа у реквизита без указанного правила"
                if default_for_type[pko.src] == code
                else ""
            )
            yield _finding(
                "правило не вызывается", "ПКО", code,
                "на правило не ссылается ни ветка выгрузки, ни реквизит, ни код обработчиков" + note,
            )
            continue
        if (
            pko.src.startswith(_REF_CREATING)
            and code not in from_branches
            and code not in from_code
            and props
            and not any(p["целиком"] for p in props)
            and pko.src not in on_demand
        ):
            shown = ", ".join(
                f"{p['правило']} / {p.get('табличная часть') + '.' if p.get('табличная часть') else ''}"
                f"{p['реквизит']}"
                for p in props[:3]
            ) + (f" и ещё {len(props) - 3}" if len(props) > 3 else "")
            yield _finding(
                "только ссылкой", "ПКО", code,
                f"своей ветки выгрузки нет, а реквизиты ({shown}) не ставят «ВыгрузитьОбъект ="
                " Истина»; режима выгрузки «при необходимости» в правилах регистрации объекта"
                " нет. Для справочников, которые приёмник находит по полям поиска, это штатно",
            )


def _search_fields(pko: Pko) -> tuple[set[str], bool]:
    """(имена приёмника свойств поиска верхнего уровня, есть ли вообще ПКС с поиском).

    Свойства табличных частей в поиск объекта не входят. ПКС с параметром уходит
    параметром, а не ключом поиска.
    """
    top = [p for p in pko.props if not p.disabled and p.search]
    return {p.dst.casefold() for p in top if p.dst and not p.param}, bool(top)


def _search(d: Direction) -> Iterator[dict]:
    """Формы поиска объекта в приёмнике, из-за которых дубли, слияние или мёртвый обработчик."""
    for pko in d.pko.values():
        fields, flagged = _search_fields(pko)
        sync = "СинхронизироватьПоИдентификатору" in pko.flags
        cont = "ПродолжитьПоискПоПолямПоискаЕслиПоИдентификаторуНеНашли" in pko.flags
        handler = pko.handlers.get("ПоследовательностьПолейПоиска", "")
        creates = pko.dst.startswith(_REF_CREATING)
        code = pko.code
        if creates and not sync and not flagged and not handler:
            yield _finding(
                "поиск: нет ключей", "ПКО", code,
                "нет синхронизации по идентификатору, нет реквизитов с признаком поиска и нет"
                " обработчика поиска",
            )
        if (not sync or cont) and fields == {"этогруппа"}:
            yield _finding(
                "поиск: ключ только ЭтоГруппа", "ПКО", code,
                "единственное поле поиска — «ЭтоГруппа»: условие только по признаку группы,"
                " берётся первая найденная строка",
            )
        if handler and sync and not cont:
            yield _finding(
                "поиск: обработчик не вызывается", "ПКО", code,
                "включена синхронизация по идентификатору без продолжения поиска по полям —"
                " до обработчика поиска дело не доходит",
                обработчик="ПоследовательностьПолейПоиска",
            )
        if cont and not fields and not handler and creates and "НеСоздаватьЕслиНеНайден" not in pko.flags:
            yield _finding(
                "поиск: продолжение без полей", "ПКО", code,
                "включено продолжение поиска по полям, но полей поиска и обработчика поиска нет",
            )
        if handler and not (sync and not cont):
            yield from _search_names(pko, handler, fields)
        if handler:
            yield from _search_params(pko, handler)


def _search_names(pko: Pko, handler: str, fields: set[str]) -> Iterator[dict]:
    """Имена в литерале СтрокаИменСвойствПоиска, которых нет среди полей поиска."""
    live = "\n".join(_code(handler))
    known = fields | {(a or b).casefold() for a, b in _SEARCH_WRITE.findall(live)}
    unknown: dict[str, int] = {}
    for match in _SEARCH_STRING.finditer(live):
        rhs = match.group(1).strip()
        if not re.fullmatch(r'"(?:[^"]|"")*"', rhs):
            continue  # собрано кодом — что попадёт в условие, не видно
        for name in re.split(r"[, ]+", rhs[1:-1].replace('""', '"')):
            if name and name.casefold() not in _SYSTEM_SEARCH_NAMES | known:
                unknown.setdefault(name, live.count("\n", 0, match.start(1)) + 1)
    for name, line in unknown.items():
        yield _finding(
            "поиск: имя не из полей поиска", "ПКО", pko.code,
            f"обработчик поиска задаёт поле «{name}», а реквизита с признаком поиска с таким"
            " именем у правила нет",
            обработчик="ПоследовательностьПолейПоиска", строка=line,
        )


def _search_params(pko: Pko, handler: str) -> Iterator[dict]:
    """Параметры, которые обработчик поиска читает, против реквизитов, которые их передают."""
    passed: dict[str, bool] = {}
    for prop in pko.props:
        if not prop.disabled and prop.param:
            key = prop.param.casefold()
            passed[key] = passed.get(key, False) or prop.search
    live = "\n".join(_code(handler))
    seen: set[str] = set()
    for match in _PARAMETER.finditer(live):
        name = match.group(1) or match.group(2)
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        at = {"обработчик": "ПоследовательностьПолейПоиска", "строка": live.count("\n", 0, match.start()) + 1}
        search = passed.get(name.casefold())
        if search is None:
            yield _finding(
                "поиск: параметр не передаётся", "ПКО", pko.code,
                f"обработчик поиска читает параметр «{name}», но ни один включённый реквизит"
                " верхнего уровня его не передаёт",
                **at,
            )
        elif not search:
            yield _finding(
                "поиск: параметр без признака поиска", "ПКО", pko.code,
                f"обработчик поиска читает параметр «{name}», а реквизит передаёт его без"
                " признака поиска",
                **at,
            )


def _object_write(d: Direction) -> Iterator[dict]:
    """`Объект.Записать(` в «после загрузки» ПКО и в «после загрузки данных» конвертации."""
    places = [("ПКО", p.code, p.handlers.get("ПослеЗагрузки", ""), "ПослеЗагрузки") for p in d.pko.values()]
    if d.conversion is not None:
        places.append(
            ("Конвертация", CONVERSION_CODE, d.conversion.handlers.get("ПослеЗагрузкиДанных", ""),
             "ПослеЗагрузкиДанных")
        )
    for kind, code, text, handler in places:
        lines = [n for n, line in enumerate(_code(text), 1) if _OBJECT_WRITE.search(_masked(line))]
        if lines:
            yield _finding(
                "запись объекта после загрузки", kind, code,
                f"в «{handler}» объект записывается сам («Объект.Записать»)",
                обработчик=handler, строки=lines,
            )


def run(d: Direction) -> list[dict]:
    """Все замечания направления. Считаются один раз и живут вместе с индексом."""
    if d.findings is None:
        found = [
            *_algorithms(d),
            *_dangling(d),
            *_repeats(d),
            *_branches(d),
            *_reach(d),
            *_search(d),
            *_object_write(d),
        ]
        # Алгоритм, которого никто не зовёт, не исполняется: сломанное в нём на обмен
        # не влияет, пока его не начнут вызывать.
        called = {
            name.casefold()
            for kind, code, _, _, text, _ in _texts(d)
            for line in _code(text)
            for name in _ALGORITHM_REF.findall(_masked(line))
            if not (kind == "Алгоритм" and name.casefold() == code.casefold())
        }
        for f in found:
            if f["вид правила"] == "Алгоритм" and f["правило"].casefold() not in called:
                f["уровень"] = WARNING
                f["суть"] += " (сам алгоритм нигде не вызывается — на обмен это не влияет)"
        order = list(CHECKS)
        d.findings = sorted(found, key=lambda f: order.index(f["проверка"]))
    return d.findings


def for_rule(d: Direction, kind: str, code: str) -> list[dict]:
    """Замечания одного правила."""
    return [f for f in run(d) if f["вид правила"] == kind and f["правило"] == code]
