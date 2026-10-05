"""Проверка разбора правил на реальных файлах.

Запуск:  python test_rules_index.py [каталог_с_направлениями]
По умолчанию берёт ../Data1C. Падает с понятным сообщением, если разбор сломался.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from rules_index import Index, obj_short


def main(root: Path) -> int:
    # Консоль Windows по умолчанию не в UTF-8 — иначе падает на стрелке в названии.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    assert root.is_dir(), f"нет каталога {root}"
    ix = Index(root)
    assert ix.directions, "не найдено ни одного направления"
    print(f"направлений: {len(ix.directions)}")

    for direction in ix.directions.values():
        sides = ix.sides(direction)
        assert len(sides) == 2 and all(sides), f"{direction.key}: не определились стороны"
        print(f"  {direction.key:<14} {direction.title:<20} ПКО={len(direction.pko)}")

    # Направление опознаётся по-человечески, и порядок сторон значим.
    for query, expected in [
        ("УПП → ЕРП", "УПП_ERP"),
        ("ЕРП → УПП", "ERP_УПП"),
        ("из розницы в ерп", "Розница_ERP"),
    ]:
        if expected not in ix.directions:
            continue
        got = ix.resolve(query).key
        assert got == expected, f"«{query}» опознано как {got}, ожидалось {expected}"

    upp = ix.directions.get("УПП_ERP")
    if upp is None:
        print("направления УПП_ERP нет — проверка содержимого пропущена")
        return 0

    # Правила выгрузки должны отдавать вызовы правил конвертации.
    pvds = ix.pvd_for(upp, "ПоступлениеТоваровУслуг")
    assert pvds, "не найдено правило выгрузки ПоступлениеТоваровУслуг"
    calls = pvds[0].pko_calls
    assert len(calls) > 5, f"подозрительно мало вызовов правил конвертации: {calls}"
    assert all(c in upp.pko for c in calls), (
        "правило выгрузки зовёт правила конвертации, которых нет: "
        f"{[c for c in calls if c not in upp.pko]}"
    )

    # Отбор регистрации должен разворачиваться в читаемые условия.
    pros = ix.pro_for(upp, "ПоступлениеТоваровУслуг")
    assert pros and "ВидОперации" in pros[0].obj_filter, "отбор регистрации не разобрался"
    assert pros[0].vidops, "не извлеклись виды операций"

    # Каскады через свойства — то, что не видно в ветках выгрузки.
    pko_name = "ПоступлениеТоваровУслуг_ПеремещениеТоваров"
    if pko_name in upp.pko:
        cascades = ix.cascades(upp, pko_name)
        assert cascades, "каскады не нашлись"
        assert pko_name not in cascades, "рекурсия зациклилась на самом правиле"

    # Функциональные блоки берутся из правил конвертации, а не только из выгрузки.
    blocks = ix.blocks(upp)
    assert len(blocks) > 10, f"функциональных блоков подозрительно мало: {list(blocks)}"

    # Ожидания от приёмника должны собираться без пустых имён.
    pko = upp.pko[calls[0]]
    assert obj_short(pko.dst), "у правила конвертации нет приёмника"

    # Справочники своей ветки выгрузки не имеют — их тянут подчинённые правила.
    retail = ix.directions.get("ERP_Розница")
    if retail is not None:
        assert not ix.pvd_for(retail, "Магазины"), "у Магазинов вдруг появилась своя выгрузка"
        carried = ix.carried_by(retail, "Магазины")
        assert carried, "не нашлось правило, переносящее Магазины"
        assert any(c["вызывается при выгрузке"] for c in carried), "не определилась ветка-источник"
        assert not ix.carried_by(retail, "КассыККМ"), (
            "КассыККМ в этом направлении в обмене не участвуют — так же считает "
            "и описание Описания/КассыККМ__ERP_Розница.md"
        )

    print(f"процессов: {len(blocks)}, вызовов у ПТУ: {len(calls)}")
    проверка_битая_папка(root)
    print("проверка пройдена")
    return 0


def проверка_битая_папка(root: Path) -> None:
    """Битый файл в одной папке не роняет остальные; по ней остаётся прежняя версия."""
    good = next(p for p in sorted(root.iterdir()) if (p / "ExchangeRules.xml").exists())
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(good, work / good.name)
        (work / "Битая").mkdir()
        (work / "Битая" / "ExchangeRules.xml").write_text("<не xml", encoding="utf-8")

        ix = Index(work)
        assert list(ix.directions) == [good.name], f"битая папка повлияла на остальные: {list(ix.directions)}"
        assert len(ix.errors) == 1 and ix.errors[0].startswith("Битая — "), ix.errors
        assert "пропущено" in ix.errors[0], ix.errors

        # Папка была исправной, а новый коммит её сломал — отвечаем по прежней версии.
        (work / good.name / "ExchangeRules.xml").write_bytes(b"\xff\xfe")
        again = Index(work, previous=ix)
        assert again.directions[good.name] is ix.directions[good.name], "прежняя версия не сохранилась"
        assert any(e.startswith(good.name) and "прежняя версия" in e for e in again.errors), again.errors
    print("  битая папка: остальные читаются, по ней — прежняя версия")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / ".." / "Data1C"
    raise SystemExit(main(target.resolve()))
