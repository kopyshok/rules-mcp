"""MCP-сервер для разбора правил обмена 1С (КД 2.0).

Подключается к Open WebUI как внешний сервер инструментов:
    Настройки → Админ → Интеграции → Тип «MCP (Streamable HTTP)»
    Адрес http://rules-mcp:8000/mcp, авторизация None.

Настройки через переменные окружения:
    RULES_DIR         каталог с папками направлений (по умолчанию /data/rules)
    GITLAB_REPO       адрес репозитория; пусто — работать с тем, что уже на диске
    GITLAB_TOKEN      токен на чтение
    GITLAB_BRANCH     ветка с правилами; пусто — ветка по умолчанию
    RULES_SUBDIR      подкаталог внутри репозитория, если правила лежат не в корне
    REFRESH_INTERVAL  период опроса в секундах, 0 — не опрашивать (по умолчанию 3600)
    REFRESH_PASSWORD  пароль для инструмента «обновить»
    HOST, PORT        адрес прослушивания (0.0.0.0:8000)
"""

from __future__ import annotations

__version__ = "2.1.0"  # см. CHANGELOG.md

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from fastmcp import FastMCP

from query_extract import extract_queries
from rules_index import (
    PROP_HANDLER,
    Index,
    all_props,
    branch_call_places,
    handler_names,
    handler_text,
    obj_short,
    page_lines,
    prop_handler,
    prop_target,
)

# ---------------------------------------------------------------------------
# Настройки

RULES_DIR = Path(os.getenv("RULES_DIR", "/data/rules"))
GITLAB_REPO = os.getenv("GITLAB_REPO", "").strip()
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "").strip()
GITLAB_BRANCH = os.getenv("GITLAB_BRANCH", "").strip()  # пусто — ветка по умолчанию
RULES_SUBDIR = os.getenv("RULES_SUBDIR", "").strip()
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "3600"))
REFRESH_PASSWORD = os.getenv("REFRESH_PASSWORD", "").strip()

mcp = FastMCP("Правила обмена 1С")

_lock = threading.Lock()
_index: Index | None = None
_state: dict[str, str] = {"версия": "неизвестна", "обновлено": "—", "ошибка": ""}


# ---------------------------------------------------------------------------
# Git и индекс


def _repo_url() -> str:
    """Подставляет токен в адрес репозитория, не светя его в логах."""
    if not GITLAB_TOKEN:
        return GITLAB_REPO
    parts = urlsplit(GITLAB_REPO)
    netloc = f"oauth2:{quote(GITLAB_TOKEN, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:400])
    return result.stdout.strip()


def _checkout_root() -> Path:
    """Каталог, где лежит клон репозитория (не путать с каталогом правил)."""
    return RULES_DIR.parent / "checkout" if GITLAB_REPO else RULES_DIR


def _rules_root() -> Path:
    root = _checkout_root()
    return (root / RULES_SUBDIR) if (GITLAB_REPO and RULES_SUBDIR) else root


def _branch_of(root: Path) -> str:
    """Ветка, с которой работаем: заданная настройкой либо текущая в клоне."""
    if GITLAB_BRANCH:
        return GITLAB_BRANCH
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)


def _sync_repo() -> bool:
    """Забирает свежие правила. Возвращает True, если коммит сменился."""
    if not GITLAB_REPO:
        return True  # локальный каталог: считаем, что перечитать надо

    root = _checkout_root()
    if root.exists() and not (root / ".git").exists():
        # Остаток неудавшегося клонирования: иначе клон сюда больше не встанет
        # никогда, и сервис молча застрянет на пустом индексе.
        shutil.rmtree(root)
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--depth", "1"]
        if GITLAB_BRANCH:
            args += ["--branch", GITLAB_BRANCH]
        _git(*args, _repo_url(), str(root), cwd=root.parent)
        return True

    before = _git("rev-parse", "HEAD", cwd=root)
    branch = _branch_of(root)
    _git("fetch", "--depth", "1", "origin", branch, cwd=root)
    _git("reset", "--hard", "FETCH_HEAD", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root) != before


def _describe_version(root: Path) -> str:
    try:
        commit = _git("log", "-1", "--format=%h от %cd", "--date=format:%d.%m.%Y", cwd=root)
        return f"ветка {_branch_of(root)}, коммит {commit}"
    except Exception:
        return "без версии (каталог не под git)"


def refresh(force: bool = False) -> dict:
    """Синхронизация с GitLab и пересборка индекса. Индекс подменяется целиком."""
    global _index
    try:
        changed = _sync_repo()
        if not changed and _index is not None and not force:
            _state["ошибка"] = ""
            return {"обновление": "не требовалось, правила те же", **status()}

        root = _rules_root()
        fresh = Index(root)  # строим новый, старый продолжает отвечать
        with _lock:
            _index = fresh
            _state["версия"] = _describe_version(_checkout_root())
            _state["обновлено"] = time.strftime("%d.%m.%Y %H:%M")
            _state["ошибка"] = ""
        note = (
            f"правила обновились, перечитано направлений: {len(fresh.directions)}"
            if changed
            else f"правила те же, перечитано заново: {len(fresh.directions)} направлений"
        )
        return {"обновление": note, **status()}
    except Exception as exc:  # правила не обновились — продолжаем на старом индексе
        _state["ошибка"] = str(exc)
        return {"обновление": f"не удалось: {exc}", **status()}


def status() -> dict:
    return {
        "версия сервиса": __version__,
        "версия правил": _state["версия"],
        "проверялось": _state["обновлено"],
        **({"внимание": f"обновление не проходит: {_state['ошибка']}"} if _state["ошибка"] else {}),
    }


def index() -> Index:
    with _lock:
        if _index is None:
            raise RuntimeError("индекс ещё не построен, повторите через несколько секунд")
        return _index


def _watcher() -> None:
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh()


# ---------------------------------------------------------------------------
# Инструменты


@mcp.tool
def exchange_overview(direction: str = "") -> dict:
    """Что вообще есть в правилах обмена: направления, документы, процессы.

    Без параметра — список направлений обмена. С направлением («УПП → ЕРП»,
    «из розницы в ерп», «УПП_ERP») — документы этого направления и
    функциональные блоки (процессы), которыми помечены ветки выгрузки.
    Отсюда стоит начинать, если неизвестно точное имя документа.
    """
    ix = index()
    if not direction:
        return {
            "направления": [
                {
                    "ключ": d.key,
                    "направление": d.title,
                    "правил регистрации": sum(1 for p in d.pro if not p.disabled),
                    "правил выгрузки": sum(1 for p in d.pvd if not p.disabled),
                    "правил конвертации": len(d.pko),
                }
                for d in ix.directions.values()
            ],
            **status(),
        }

    exch = ix.resolve(direction)
    docs = ix.documents(exch)

    def line(name: str, info: dict) -> str:
        marks = []
        if info["про"]:
            marks.append("регистрируется")
        if info["пвд"]:
            marks.append("выгружается")
        if not marks:
            marks.append("только как подчинённое правило")
        if info["блоки"]:
            marks.append("процессы: " + ", ".join(sorted(info["блоки"])))
        return f"{name} — {'; '.join(marks)}"

    return {
        "направление": exch.title,
        "ключ": exch.key,
        "документы": [line(n, docs[n]) for n in sorted(docs)],
        "процессы": {b: ", ".join(d) for b, d in ix.blocks(exch).items()},
        **status(),
    }


@mcp.tool
def trace_document(document: str, direction: str) -> dict:
    """Что происходит с документом при обмене — от регистрации до приёмника.

    Главный инструмент. Показывает: при каких условиях документ регистрируется
    к выгрузке, какие правила конвертации вызываются, какие документы возникают
    у приёмника, и какие ещё документы создаются каскадами через свойства
    (их не видно в самих ветках выгрузки).

    У каждого вызываемого правила — где стоит вызов: обработчик и строка, по
    ним читайте get_rule_handler. Для вызовов через общие алгоритмы — строка в
    ветке, где зовётся алгоритм, и строка в алгоритме, где вызывается правило.
    У ветки — откуда метки процессов: свои или пришли от вызываемых правил.
    Пришедшая метка не доказывает, что такая выгрузка действует.
    """
    ix = index()
    exch = ix.resolve(direction)
    pros = ix.pro_for(exch, document)
    pvds = ix.pvd_for(exch, document)
    if not pros and not pvds:
        # Своей ветки выгрузки нет. Так переносятся справочники — их тянут
        # подчинённые правила из документов. Показываем, кто именно.
        carried = ix.carried_by(exch, document)
        if carried:
            return {
                "направление": exch.title,
                "объект": document,
                "выгрузка": "своей ветки выгрузки нет — переносится подчинённым правилом",
                "переносится правилами": carried,
                "подсказка": "Состав реквизитов — инструмент get_conversion_rule по имени правила.",
                **status(),
            }
        near = [n for n in ix.documents(exch) if document.casefold() in n.casefold()]
        raise LookupError(
            f"в направлении {exch.title} правил на «{document}» нет"
            " — ни регистрации, ни выгрузки, ни конвертации."
            + (f" Похожие: {', '.join(sorted(near)[:10])}" if near else "")
        )

    registration = [
        {
            "код": p.code,
            "наименование": p.name,
            "отключено": p.disabled,
            "виды операций в отборе": p.vidops,
            "отбор по объекту": p.obj_filter or "без отбора",
            "отбор по плану обмена": p.plan_filter or "без отбора",
            "обработчики": sorted(p.handlers),
        }
        for p in pros
    ]

    branches = []
    receivers: dict[str, set[str]] = {}
    for pvd in pvds:
        called = []
        for name in pvd.pko_calls:
            pko = exch.pko.get(name)
            cascades = ix.cascades(exch, name) if pko else []
            doc_cascades = sorted(
                {
                    obj_short(exch.pko[c].dst)
                    for c in cascades
                    if c in exch.pko and exch.pko[c].dst.startswith("Документ")
                }
            )
            called.append(
                {
                    "правило конвертации": name,
                    "есть в правилах": pko is not None,
                    "отключено": bool(pko and pko.disabled),
                    "документ приёмника": obj_short(pko.dst) if pko else "—",
                    "каскадом создаются документы": doc_cascades,
                    "всего подчинённых правил": len(cascades),
                    **branch_call_places(pvd, name),
                }
            )
            if pko and not pko.disabled:
                receivers.setdefault(obj_short(pko.dst), set()).add(name)
                for extra in doc_cascades:
                    receivers.setdefault(extra, set()).add(f"каскад из {name}")
        # Вызов может стоять не в самой ветке, а в общем алгоритме, который она
        # зовёт. В коде ветки его не видно, поэтому показываем отдельно.
        via_algorithms = ix.algorithms_used(exch, "\n".join(pvd.handlers.values()))
        for algorithm, names in via_algorithms.items():
            for name in names:
                pko = exch.pko.get(name)
                if pko is None or pko.disabled:
                    continue
                receivers.setdefault(obj_short(pko.dst), set()).add(f"через алгоритм {algorithm}")
                # Каскады у них такие же, как у вызванных напрямую.
                for child in ix.cascades(exch, name):
                    sub = exch.pko.get(child)
                    if sub is not None and sub.dst.startswith("Документ"):
                        receivers.setdefault(obj_short(sub.dst), set()).add(
                            f"каскад из {name} (через алгоритм {algorithm})"
                        )
        branch = {
            "код": pvd.code,
            "отключено": pvd.disabled,
            "процессы": ix.pvd_blocks(exch, pvd),
            "откуда процессы": ix.pvd_block_sources(exch, pvd),
            "обработчики": sorted(pvd.handlers),
            "вызываемые правила конвертации": called,
        }
        if via_algorithms:
            branch["правила конвертации из общих алгоритмов"] = via_algorithms
            branch["где вызываются правила из общих алгоритмов"] = ix.algorithm_call_places(
                exch, pvd, "ПВД"
            )
        if pvd.custom_selection:
            branch["внимание"] = (
                f"отбор произвольным алгоритмом: объект выборки {obj_short(pvd.obj)}"
                " декоративен, настоящий отбор задан запросом в обработчике"
            )
        branches.append(branch)

    return {
        "направление": exch.title,
        "документ": document,
        "регистрация": registration or "правил регистрации нет — документ выгружается иначе",
        "выгрузка": branches or "правил выгрузки нет",
        "документы приёмника": {k: sorted(v) for k, v in sorted(receivers.items())},
        "подсказка": "Полный состав реквизитов правила — инструмент get_conversion_rule. "
        "Что правила ждут от конфигурации приёмника — receiver_fields.",
        **status(),
    }


@mcp.tool
def get_conversion_rule(rule: str, direction: str, verbose: bool = False) -> dict:
    """Карточка одного правила конвертации: реквизиты, табличные части, каскады.

    Показывает соответствия «реквизит источника → реквизит приёмника» с типами,
    отмечает поля поиска, отключённые строки и реквизиты, заполняемые алгоритмом
    (у них пустой источник). С verbose=True добавляет тексты обработчиков —
    они объёмные, поэтому по умолчанию выключены.
    """
    ix = index()
    exch = ix.resolve(direction)
    pko = ix.find_pko(exch, rule)
    if pko is None:
        near = [c for c in exch.pko if rule.casefold() in c.casefold()]
        raise LookupError(
            f"правила конвертации «{rule}» в направлении {exch.title} нет."
            + (f" Похожие: {', '.join(sorted(near)[:10])}" if near else "")
        )

    def row(prop) -> dict:
        item = {
            "источник": prop.src or "— заполняется алгоритмом",
            "приёмник": prop.dst,
            "тип приёмника": prop.dst_type,
        }
        if prop.conv_rule:
            item["через правило"] = prop.conv_rule
        if prop.search:
            item["поле поиска"] = True
        if prop.disabled:
            item["отключено"] = True
        if prop.param:
            item["параметр"] = prop.param
        if prop.handler:
            item["есть обработчик"] = True
            item["код реквизита"] = prop.code
        return item

    card = {
        "направление": exch.title,
        "правило": pko.code,
        "наименование": pko.name,
        "отключено": pko.disabled,
        "источник": pko.src,
        "приёмник": pko.dst,
        "процессы": pko.blocks,
        "шапка": [row(p) for p in pko.props],
        "табличные части": [
            {
                "источник": s.src,
                "приёмник": s.dst,
                "строки": [row(p) for p in s.props],
            }
            for s in pko.sections
        ],
        "каскады в подчинённые правила": ix.cascades(exch, pko.code),
        "обработчики": sorted(pko.handlers),
        **status(),
    }
    if verbose:
        # Молча обрезанного текста быть не должно: у самых крупных правил
        # обработчики вместе дают под 63 000 знаков и ответ перестаёт доходить.
        spent = sum(len(t) for t in pko.handlers.values())
        if spent > _HANDLER_TEXT_BUDGET:
            card["тексты обработчиков"] = f"не вложены: вместе они {spent} знаков"
            card["размеры обработчиков"] = {
                name: len(pko.handlers[name].splitlines())
                for name in handler_names("ПКО", pko.handlers)
            }
            card["как прочитать"] = (
                "get_rule_handler с этим правилом и нужным обработчиком — он отдаёт"
                " текст страницами и сообщает, всё ли показано"
            )
        else:
            card["тексты обработчиков"] = pko.handlers
    return card


@mcp.tool
def get_rule_handler(
    rule: str,
    direction: str,
    rule_kind: str = "",
    handler: str = "",
    start_line: int = 1,
    max_lines: int = 500,
    prop: str = "",
) -> dict:
    """Полный текст обработчика правила — постранично, с номерами строк.

    Условия, по которым ветка выгрузки выбирает правило конвертации, лежат в
    коде обработчиков. По одной найденной строке их восстановить нельзя, нужен
    весь текст.

    Правило можно назвать кодом, наименованием или именем объекта: у правил
    регистрации коды числовые, и «ПеремещениеТоваров» для них понятнее, чем
    «000000039». Подошло несколько правил — в ответе перечень, выберите код.

    Вид правила (ПВД, ПКО, ПРО, Алгоритм, Запрос) можно не указывать, если и так
    однозначно. Коды веток выгрузки и правил конвертации местами совпадают —
    тогда вид спросят. Читаются и общие алгоритмы с именованными запросами: у
    них один обработчик с именем «Текст».

    Без параметра handler возвращает перечень обработчиков правила и размер
    каждого — с этого стоит начинать. Если показано не всё, в ответе будет
    «следующая строка»: повторный вызов с этим start_line даёт продолжение.

    prop — обработчик отдельного реквизита правила конвертации: там лежит
    условие заполнения реквизита, отдельное от условий выбора правила.
    Реквизит называется кодом (надёжно) или именем приёмника (если оно в
    правиле одно). Обработчик у реквизита один, ПередВыгрузкой: handler можно
    не указывать, а указанный другой — ошибка. Какие реквизиты имеют
    обработчики, видно в перечне обработчиков правила конвертации.
    """
    ix = index()
    exch = ix.resolve(direction)
    kind, found = ix.find_rule(exch, rule, rule_kind or ("ПКО" if prop else ""))
    if prop and kind != "ПКО":
        raise LookupError("обработчики реквизитов есть только у правил конвертации (ПКО)")
    head = {
        "направление": exch.title,
        "вид правила": kind,
        "правило": found.code,
        "наименование": found.name,
        "отключено": found.disabled,
    }
    if prop:
        section, row = prop_handler(found, prop, handler)
        return {
            **head,
            "реквизит": prop_target(row),
            "код реквизита": row.code,
            **({"табличная часть": section} if section else {}),
            "обработчик": PROP_HANDLER,
            **page_lines(row.handler, start_line, max_lines),
            **status(),
        }
    if not handler:
        listing = {
            **head,
            "обработчики": {
                name: len(found.handlers[name].splitlines())
                for name in handler_names(kind, found.handlers)
            },
            "подсказка": "Текст — повторный вызов с параметром handler.",
        }
        if kind == "ПКО":
            # Без этого об обработчиках реквизитов не догадаться: их больше,
            # чем обработчиков самих правил.
            own = {
                row.code: f"{section + '.' if section else ''}{prop_target(row)}, "
                f"строк {len(row.handler.splitlines())}"
                for section, row in all_props(found)
                if row.handler
            }
            if own:
                listing["обработчики реквизитов"] = own
                listing["подсказка"] += " Обработчик реквизита — параметр prop с его кодом."
        return {**listing, **status()}

    name, text = handler_text(found, kind, handler)
    return {**head, "обработчик": name, **page_lines(text, start_line, max_lines), **status()}


_QUERY_TEXT_BUDGET = 60_000  # дальше ответ перестаёт помещаться в чат
_HANDLER_TEXT_BUDGET = 40_000


def _query_card(name: str, use) -> dict:
    """Одно использование запроса: где задан, чем выполнен, из чего состоит."""
    card = {
        "обработчик": name,
        "переменная": use.variable,
        "строка текста": use.assign_line,
        "способ выполнения": use.exec_kind or "не найден",
        "извлечён полностью": use.complete,
        "параметры": [
            {"имя": p.name, "выражение": p.expression, "строка": p.line} for p in use.params
        ],
        "пакет": [
            {
                "номер": q.index,
                "создаёт таблицу": q.temp_table,
                "зависит от": q.depends_on,
                "результат в переменных": q.result_vars,
                "текст": q.text,
            }
            for q in use.packet
        ],
        "текст запроса": use.text,
    }
    if use.exec_line:
        card["строка выполнения"] = use.exec_line
    if use.unresolved:
        card["номер не определён"] = [
            {"переменная": u.variable, "выражение": u.expression, "строка": u.line}
            for u in use.unresolved
        ]
    return card


@mcp.tool
def get_rule_queries(
    rule: str,
    direction: str,
    rule_kind: str = "",
    handler: str = "",
) -> dict:
    """Запросы из обработчиков правила: пакет, параметры, временные таблицы.

    Разбирает код обработчика и достаёт запросы: делит пакет на отдельные
    запросы, сопоставляет `Результат[n]` с номером запроса в пакете, показывает,
    какая временная таблица где создаётся и от каких зависит.

    Текст, который не сводится к строковому литералу (собран через
    СтрСоединить, склейкой или взят из переменной), полным не выдаётся: такой
    запрос помечен неполным, и его надо читать глазами через get_rule_handler.
    Номер элемента пакета, вычисляемый в коде, не угадывается, а выносится
    отдельно как неопределённый.
    """
    ix = index()
    exch = ix.resolve(direction)
    kind, found = ix.find_rule(exch, rule, rule_kind)
    names = (
        [handler_text(found, kind, handler)[0]]
        if handler
        else handler_names(kind, found.handlers)
    )

    cards: list[dict] = []
    missing: list[dict] = []
    for name in names:
        for use in extract_queries(found.handlers[name]):
            if use.complete:
                cards.append(_query_card(name, use))
            else:
                missing.append(
                    {
                        "обработчик": name,
                        "переменная": use.variable,
                        "строка": use.assign_line,
                        "причина": use.reason,
                    }
                )

    # Тексты запросов объёмные: структуру отдаём всегда, тексты — пока влезают.
    spent = sum(
        len(q["текст"] or "") for c in cards for q in c["пакет"]
    ) + sum(len(c["текст запроса"] or "") for c in cards)
    dropped = spent > _QUERY_TEXT_BUDGET
    if dropped:
        for card in cards:
            card["текст запроса"] = None
            for part in card["пакет"]:
                part["текст"] = None

    return {
        "направление": exch.title,
        "вид правила": kind,
        "правило": found.code,
        "запросы": cards,
        **({"не извлечено полностью": missing} if missing else {}),
        **(
            {
                "тексты опущены по размеру": True,
                "подсказка": "Структура полная, тексты читайте через get_rule_handler"
                " или запросите один обработчик параметром handler.",
            }
            if dropped
            else {}
        ),
        **status(),
    }


@mcp.tool
def search_rules(
    query: str,
    direction: str = "",
    rule_kind: str = "",
    rule: str = "",
    handler: str = "",
    limit: int = 40,
    lines_per_handler: int = 5,
) -> dict:
    """Где в правилах упоминается реквизит, алгоритм или фрагмент логики.

    Ищет по соответствиям реквизитов, отборам регистрации и текстам
    обработчиков всех видов правил. Без направления — по всем сразу. Нужен,
    когда неизвестно, в каком правиле искать: «где вообще трогается СкладОрдер».

    Отвечает фрагментами строк, а не логикой целиком. По одной строке судить о
    том, при каких условиях вызывается правило конвертации, нельзя: у каждого
    попадания есть номер строки, по нему читайте обработчик через
    get_rule_handler. Закомментированный код в выдачу не попадает.

    rule — код, наименование или имя объекта правила: правило регистрации
    можно назвать именем документа. Подошло несколько правил — ищется по всем.
    Правила нет ни в одном направлении — ошибка с похожими.
    """
    ix = index()
    targets = [ix.resolve(direction)] if direction else list(ix.directions.values())
    hits = ix.search(targets, query, rule_kind, rule, handler, limit, lines_per_handler)
    return {
        "найдено": hits,
        "всего": len(hits),
        **({"обрезано по лимиту": True} if len(hits) >= limit else {}),
        "подсказка": "Полный текст — get_rule_handler с тем же правилом и обработчиком.",
        **status(),
    }


@mcp.tool
def receiver_fields(document: str, direction: str, obj: str = "") -> dict:
    """Что правила ждут от конфигурации приёмника: реквизиты и их типы.

    Нужен, чтобы сверить с реальным составом объекта в конфигурации приёмника
    (через сервер конфигураций) и найти реквизиты, которых там нет или у которых
    сменился тип — это даёт тихую потерю данных: обмен не падает, но не переносит.

    По умолчанию показывает документы приёмника, а справочники — только списком
    имён, иначе ответ разрастается. Чтобы получить состав конкретного объекта,
    передайте его в obj («Номенклатура», «ПриобретениеТоваровУслуг»).
    """
    ix = index()
    exch = ix.resolve(direction)
    pvds = ix.pvd_for(exch, document)
    if not pvds:
        raise LookupError(f"в направлении {exch.title} нет правил выгрузки «{document}»")

    wanted: set[str] = set()
    for pvd in pvds:
        for name in pvd.pko_calls:
            wanted.add(name)
            wanted.update(ix.cascades(exch, name))

    by_object: dict[str, dict[str, dict]] = {}
    for name in sorted(wanted):
        pko = exch.pko.get(name)
        if pko is None or pko.disabled:
            continue
        target = by_object.setdefault(pko.dst, {})
        groups = [("", pko.props)] + [(s.dst, s.props) for s in pko.sections]
        for section, props in groups:
            for prop in props:
                if prop.disabled or not prop.dst:
                    continue
                key = f"{section}.{prop.dst}" if section else prop.dst
                entry = target.setdefault(key, {"тип": prop.dst_type, "правила": set()})
                if prop.dst_type and prop.dst_type not in entry["тип"]:
                    entry["тип"] = f"{entry['тип']} / {prop.dst_type}".strip(" /")
                entry["правила"].add(name)

    def fields_of(obj: str) -> list[str]:
        """Одна строка на реквизит: имя, тип и — если спорный — правила."""
        out = []
        for key, info in sorted(by_object[obj].items()):
            line = f"{key}: {info['тип'] or 'тип не указан'}"
            if "/" in info["тип"]:
                line += "  ← разные типы в правилах: " + ", ".join(sorted(info["правила"]))
            out.append(line)
        return out

    if obj:
        matches = [o for o in by_object if obj_short(o).casefold() == obj.casefold()]
        if not matches:
            raise LookupError(
                f"среди объектов приёмника «{obj}» нет. "
                f"Есть: {', '.join(sorted(obj_short(o) for o in by_object))}"
            )
        return {
            "направление": exch.title,
            "объект приёмника": matches[0],
            "реквизиты": fields_of(matches[0]),
            **status(),
        }

    documents = {o: f for o, f in by_object.items() if o.startswith("Документ")}
    others = sorted(obj_short(o) for o in by_object if not o.startswith("Документ"))
    return {
        "направление": exch.title,
        "документ": document,
        "документы приёмника": [
            {"объект": obj_short(obj), "реквизиты": fields_of(obj)}
            for obj in sorted(documents)
        ],
        "справочники и прочее (состав по запросу)": others,
        "как пользоваться": "Возьмите состав этих объектов из конфигурации приёмника и "
        "сравните: чего нет, что переименовано, у чего другой тип. Состав справочника — "
        "повторный вызов с параметром obj.",
        **status(),
    }


@mcp.tool
def refresh_rules(password: str) -> dict:
    """Перечитать правила из GitLab прямо сейчас, не дожидаясь опроса.

    Правила и так подтягиваются автоматически. Инструмент нужен, когда только
    что выложили релиз. Пароль спрашивается у человека — чтобы обновление не
    запускалось само по себе.
    """
    if not REFRESH_PASSWORD:
        raise PermissionError("обновление по запросу отключено: пароль не задан в настройках")
    if password != REFRESH_PASSWORD:
        raise PermissionError("неверный пароль")
    return refresh(force=True)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    refresh(force=True)
    if REFRESH_INTERVAL > 0:
        threading.Thread(target=_watcher, daemon=True).start()
    mcp.run(
        transport="http",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )
