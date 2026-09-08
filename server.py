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

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from fastmcp import FastMCP

from rules_index import Index, obj_short

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
def spravochnik(napravlenie: str = "") -> dict:
    """Что вообще есть в правилах обмена: направления, документы, процессы.

    Без параметра — список направлений обмена. С направлением («УПП → ЕРП»,
    «из розницы в ерп», «УПП_ERP») — документы этого направления и
    функциональные блоки (процессы), которыми помечены ветки выгрузки.
    Отсюда стоит начинать, если неизвестно точное имя документа.
    """
    ix = index()
    if not napravlenie:
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

    direction = ix.resolve(napravlenie)
    docs = ix.documents(direction)

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
        "направление": direction.title,
        "ключ": direction.key,
        "документы": [line(n, docs[n]) for n in sorted(docs)],
        "процессы": {b: ", ".join(d) for b, d in ix.blocks(direction).items()},
        **status(),
    }


@mcp.tool
def trassirovka(dokument: str, napravlenie: str) -> dict:
    """Что происходит с документом при обмене — от регистрации до приёмника.

    Главный инструмент. Показывает: при каких условиях документ регистрируется
    к выгрузке, какие правила конвертации вызываются, какие документы возникают
    у приёмника, и какие ещё документы создаются каскадами через свойства
    (их не видно в самих ветках выгрузки).
    """
    ix = index()
    direction = ix.resolve(napravlenie)
    pros = ix.pro_for(direction, dokument)
    pvds = ix.pvd_for(direction, dokument)
    if not pros and not pvds:
        # Своей ветки выгрузки нет. Так переносятся справочники — их тянут
        # подчинённые правила из документов. Показываем, кто именно.
        carried = ix.carried_by(direction, dokument)
        if carried:
            return {
                "направление": direction.title,
                "объект": dokument,
                "выгрузка": "своей ветки выгрузки нет — переносится подчинённым правилом",
                "переносится правилами": carried,
                "подсказка": "Состав реквизитов — инструмент pravilo по имени правила.",
                **status(),
            }
        near = [n for n in ix.documents(direction) if dokument.casefold() in n.casefold()]
        raise LookupError(
            f"в направлении {direction.title} правил на «{dokument}» нет"
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
            pko = direction.pko.get(name)
            cascades = ix.cascades(direction, name) if pko else []
            doc_cascades = sorted(
                {
                    obj_short(direction.pko[c].dst)
                    for c in cascades
                    if c in direction.pko and direction.pko[c].dst.startswith("Документ")
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
                }
            )
            if pko and not pko.disabled:
                receivers.setdefault(obj_short(pko.dst), set()).add(name)
                for extra in doc_cascades:
                    receivers.setdefault(extra, set()).add(f"каскад из {name}")
        branches.append(
            {
                "код": pvd.code,
                "отключено": pvd.disabled,
                "процессы": ix.pvd_blocks(direction, pvd),
                "обработчики": sorted(pvd.handlers),
                "вызываемые правила конвертации": called,
            }
        )

    return {
        "направление": direction.title,
        "документ": dokument,
        "регистрация": registration or "правил регистрации нет — документ выгружается иначе",
        "выгрузка": branches or "правил выгрузки нет",
        "документы приёмника": {k: sorted(v) for k, v in sorted(receivers.items())},
        "подсказка": "Полный состав реквизитов правила — инструмент pravilo. "
        "Что правила ждут от конфигурации приёмника — ozhidaniya_priemnika.",
        **status(),
    }


@mcp.tool
def pravilo(imya: str, napravlenie: str, podrobno: bool = False) -> dict:
    """Карточка одного правила конвертации: реквизиты, табличные части, каскады.

    Показывает соответствия «реквизит источника → реквизит приёмника» с типами,
    отмечает поля поиска, отключённые строки и реквизиты, заполняемые алгоритмом
    (у них пустой источник). С podrobno=True добавляет тексты обработчиков —
    они объёмные, поэтому по умолчанию выключены.
    """
    ix = index()
    direction = ix.resolve(napravlenie)
    pko = ix.find_pko(direction, imya)
    if pko is None:
        near = [c for c in direction.pko if imya.casefold() in c.casefold()]
        raise LookupError(
            f"правила конвертации «{imya}» в направлении {direction.title} нет."
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
        if prop.handler:
            item["есть обработчик"] = True
        return item

    card = {
        "направление": direction.title,
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
        "каскады в подчинённые правила": ix.cascades(direction, pko.code),
        "обработчики": sorted(pko.handlers),
        **status(),
    }
    if podrobno:
        card["тексты обработчиков"] = pko.handlers
    return card


@mcp.tool
def poisk(tekst: str, napravlenie: str = "", limit: int = 40) -> dict:
    """Где в правилах упоминается реквизит, алгоритм или фрагмент логики.

    Ищет по соответствиям реквизитов, отборам регистрации и текстам
    обработчиков. Без направления — по всем сразу. Нужен, когда неизвестно,
    в каком правиле искать: «где вообще трогается СкладОрдер».
    """
    ix = index()
    targets = (
        [ix.resolve(napravlenie)] if napravlenie else list(ix.directions.values())
    )
    needle = tekst.casefold()
    hits: list[dict] = []

    def add(direction, where: str, what: str, snippet: str = "") -> bool:
        hits.append(
            {
                "направление": direction.key,
                "где": where,
                "что": what,
                **({"фрагмент": snippet} if snippet else {}),
            }
        )
        return len(hits) >= limit

    for direction in targets:
        for pko in direction.pko.values():
            props = list(pko.props) + [p for s in pko.sections for p in s.props]
            for prop in props:
                if needle in f"{prop.src} {prop.dst} {prop.dst_type}".casefold():
                    if add(
                        direction,
                        f"правило конвертации {pko.code}",
                        f"{prop.src or '—'} → {prop.dst} ({prop.dst_type})",
                    ):
                        return {"найдено": hits, "обрезано по лимиту": True, **status()}
            for name, code in pko.handlers.items():
                for line in _matching_lines(code, needle):
                    if add(direction, f"правило конвертации {pko.code}", name, line):
                        return {"найдено": hits, "обрезано по лимиту": True, **status()}
        for pvd in direction.pvd:
            for name, code in pvd.handlers.items():
                for line in _matching_lines(code, needle):
                    if add(direction, f"выгрузка {obj_short(pvd.obj)}", name, line):
                        return {"найдено": hits, "обрезано по лимиту": True, **status()}
        for pro in direction.pro:
            if needle in pro.obj_filter.casefold():
                if add(direction, f"регистрация {obj_short(pro.obj)}", f"ПРО {pro.code}, отбор"):
                    return {"найдено": hits, "обрезано по лимиту": True, **status()}

    return {"найдено": hits, "всего": len(hits), **status()}


def _matching_lines(code: str, needle: str, limit: int = 3) -> list[str]:
    out = []
    for line in code.splitlines():
        stripped = line.strip()
        if needle in stripped.casefold() and not stripped.startswith("//"):
            out.append(stripped[:200])
            if len(out) >= limit:
                break
    return out


@mcp.tool
def ozhidaniya_priemnika(dokument: str, napravlenie: str, obyekt: str = "") -> dict:
    """Что правила ждут от конфигурации приёмника: реквизиты и их типы.

    Нужен, чтобы сверить с реальным составом объекта в конфигурации приёмника
    (через сервер конфигураций) и найти реквизиты, которых там нет или у которых
    сменился тип — это даёт тихую потерю данных: обмен не падает, но не переносит.

    По умолчанию показывает документы приёмника, а справочники — только списком
    имён, иначе ответ разрастается. Чтобы получить состав конкретного объекта,
    передайте его в obyekt («Номенклатура», «ПриобретениеТоваровУслуг»).
    """
    ix = index()
    direction = ix.resolve(napravlenie)
    pvds = ix.pvd_for(direction, dokument)
    if not pvds:
        raise LookupError(f"в направлении {direction.title} нет правил выгрузки «{dokument}»")

    wanted: set[str] = set()
    for pvd in pvds:
        for name in pvd.pko_calls:
            wanted.add(name)
            wanted.update(ix.cascades(direction, name))

    by_object: dict[str, dict[str, dict]] = {}
    for name in sorted(wanted):
        pko = direction.pko.get(name)
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

    if obyekt:
        matches = [o for o in by_object if obj_short(o).casefold() == obyekt.casefold()]
        if not matches:
            raise LookupError(
                f"среди объектов приёмника «{obyekt}» нет. "
                f"Есть: {', '.join(sorted(obj_short(o) for o in by_object))}"
            )
        return {
            "направление": direction.title,
            "объект приёмника": matches[0],
            "реквизиты": fields_of(matches[0]),
            **status(),
        }

    documents = {o: f for o, f in by_object.items() if o.startswith("Документ")}
    others = sorted(obj_short(o) for o in by_object if not o.startswith("Документ"))
    return {
        "направление": direction.title,
        "документ": dokument,
        "документы приёмника": [
            {"объект": obj_short(obj), "реквизиты": fields_of(obj)}
            for obj in sorted(documents)
        ],
        "справочники и прочее (состав по запросу)": others,
        "как пользоваться": "Возьмите состав этих объектов из конфигурации приёмника и "
        "сравните: чего нет, что переименовано, у чего другой тип. Состав справочника — "
        "повторный вызов с параметром obyekt.",
        **status(),
    }


@mcp.tool
def obnovit(parol: str) -> dict:
    """Перечитать правила из GitLab прямо сейчас, не дожидаясь опроса.

    Правила и так подтягиваются автоматически. Инструмент нужен, когда только
    что выложили релиз. Пароль спрашивается у человека — чтобы обновление не
    запускалось само по себе.
    """
    if not REFRESH_PASSWORD:
        raise PermissionError("обновление по запросу отключено: пароль не задан в настройках")
    if parol != REFRESH_PASSWORD:
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
