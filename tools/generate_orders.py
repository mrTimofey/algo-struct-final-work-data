#!/usr/bin/env python3
"""Генератор потока заказов для итогового проекта.

Берёт здания с адресами из выгрузки района, собирает канонический адрес и
портит его набором преобразований. На выходе получается поток заказов в
свободной форме и, отдельным файлом, разметка с правильными ответами.

  python3 generate_orders.py --district data/akademichesky --seed 4242 \
      --count 200 --variant base --out orders/ --with-truth

Разметка пишется только при флаге --with-truth. На защите генератор
запускается без него, и правильные ответы остаются у преподавателя.

Результат полностью определяется значением --seed, сценарием --variant и
списком чужих улиц: при одинаковых параметрах файлы совпадают побайтово.

Файлы на выходе:

  orders.csv     order_id, text, weight_kg
  scenario.json  сценарий доставки: депо, число курьеров, вместимость
  truth.csv      order_id, building_id, street, housenumber, noise_classes,
                 time_from, time_to      (только с --with-truth)
  run.json       параметры запуска и статистика

Временное окно намеренно живёт только внутри текста заказа: его извлечение
регулярным выражением входит в задание.
"""

import argparse
import csv
import json
import math
import os
import random
import sys

GENERATOR_VERSION = 1

# --------------------------------------------------------------------------
# Словари для порчи адреса
# --------------------------------------------------------------------------

# Родовое слово улицы: варианты написания и склонения.
STREET_KINDS = {
    "улица": {
        "short": ["ул.", "ул", "у."],
        "prep": "улице",
        "gen": "улицы",
    },
    "проспект": {
        "short": ["пр-т", "просп.", "пр."],
        "prep": "проспекте",
        "gen": "проспекта",
    },
    "переулок": {
        "short": ["пер.", "пер"],
        "prep": "переулке",
        "gen": "переулка",
    },
    "бульвар": {
        "short": ["б-р", "бул."],
        "prep": "бульваре",
        "gen": "бульвара",
    },
    "шоссе": {
        "short": ["ш.", "ш"],
        "prep": "шоссе",
        "gen": "шоссе",
    },
    "площадь": {
        "short": ["пл.", "пл"],
        "prep": "площади",
        "gen": "площади",
    },
    "проезд": {
        "short": ["пр-д", "пр."],
        "prep": "проезде",
        "gen": "проезда",
    },
    "набережная": {
        "short": ["наб.", "наб"],
        "prep": "набережной",
        "gen": "набережной",
    },
}

HOUSE_PREFIXES = ["", "д. ", "д.", "дом ", "д ", "№"]

# Соседние клавиши в раскладке ЙЦУКЕН. Опечатки по соседству выглядят
# правдоподобнее, чем равномерная замена случайного символа.
KEYBOARD_NEIGHBOURS = {
    "й": "цфы", "ц": "йуывф", "у": "цкавы", "к": "уепав", "е": "кнрпа",
    "н": "егрош", "г": "ншорл", "ш": "гщлрд",
    "щ": "шзджл", "з": "щхэжд", "х": "зъюэж", "ъ": "хэю",
    "ф": "йцыяч", "ы": "фцувячс", "в": "укасчм", "а": "векмпи",
    "п": "енримт", "р": "нгпоть", "о": "гшрлтб", "л": "шщодбю",
    "д": "щзлжюэ", "ж": "зхдэъ", "э": "хъжю",
    "я": "фыч", "ч": "ясвыц", "с": "чмвау", "м": "сивак", "и": "мтапе",
    "т": "иьпрн", "ь": "тбромг", "б": "ьюлотш", "ю": "бдэлщ",
}

HOMOGLYPHS = {
    "о": "o", "а": "a", "е": "e", "с": "c", "р": "p",
    "у": "y", "х": "x", "к": "k", "м": "m", "т": "t",
}

# Лишний текст вокруг адреса. Пусть его будет много: три шаблона на весь
# поток превращают слой распознавания в подбор трёх регулярных выражений.
COMMENT_PREFIXES = [
    "срочно", "заказ", "доставка", "клиент просил",
    "по возможности быстрее", "повторная доставка",
]

COMMENT_SUFFIXES = [
    "домофон не работает",
    "код домофона {code}",
    "{entrance} подъезд",
    "подъезд {entrance}",
    "этаж {floor}",
    "кв. {flat}",
    "квартира {flat}",
    "позвонить за {minutes} минут",
    "позвоните заранее",
    "оставить у двери",
    "не работает лифт",
    "вход со двора",
    "шлагбаум, звонить охране",
]

TIME_PHRASES = [
    "с {a}:00 до {b}:00",
    "после {a}:00",
    "до {b}:00",
    "строго с {a}:00 до {b}:00",
    "{a}:00-{b}:00",
    "удобно после {a}:00",
]

# Улицы для заказов с несуществующим адресом. Из списка берутся только те,
# которых в районе нет. Список виден каждому, кто открыл этот файл, поэтому
# на защите преподаватель подставляет свой через --foreign-streets.
FOREIGN_STREETS = [
    "улица Пушкина", "улица Гагарина", "улица Строителей", "улица Мира",
    "улица Победы", "улица Садовая", "улица Полевая", "улица Заречная",
    "улица Молодёжная", "улица Школьная", "улица Лесная", "улица Речная",
    "улица Северная", "улица Южная", "улица Западная", "улица Восточная",
    "проспект Космонавтов", "проспект Металлургов", "переулок Тихий",
    "бульвар Энтузиастов", "улица Кооперативная", "улица Вокзальная",
]

NOISE_CLASSES = ["format", "morph", "typo", "case_space", "homoglyph", "extra_text"]

# Веса выбора класса шума. Меняются между потоками, чтобы прошлогодние
# решения не оказывались готовым ответом.
NOISE_WEIGHTS = {
    "format": 5,
    "morph": 3,
    "typo": 5,
    "case_space": 4,
    "homoglyph": 2,
    "extra_text": 5,
}

# Сценарии доставки. Решение обязано работать на любом из них без правки
# кода: на защите сценарий выбирает преподаватель.
#
#   depot     где стоит депо: в центре района или на его окраине;
#   couriers  число курьеров, то есть число маршрутов;
#   capacity  ограничена ли вместимость курьера в килограммах.
VARIANTS = {
    "base": {
        "depot": "center",
        "couriers": 4,
        "capacity": False,
        "about": "депо в центре района, четыре курьера, вместимость не ограничена",
    },
    "single": {
        "depot": "center",
        "couriers": 1,
        "capacity": False,
        "about": "депо в центре района, один курьер объезжает все заказы",
    },
    "capacity": {
        "depot": "center",
        "couriers": 4,
        "capacity": True,
        "about": "депо в центре района, четыре курьера, вместимость ограничена",
    },
    "edge": {
        "depot": "edge",
        "couriers": 3,
        "capacity": False,
        "about": "депо на окраине района, три курьера, вместимость не ограничена",
    },
}

# Депо ставится только туда, где в этом радиусе все узлы дорожной сети лежат
# в наибольшей сильно связной компоненте. Тогда любая разумная привязка депо
# к сети даёт узел, из которого достижимо всё и в который можно вернуться.
DEPOT_RADIUS_M = 200

# Запас вместимости над средней загрузкой курьера в сценарии capacity. При
# двадцати процентах группы, нарезанные по геометрии, перегружены в типичном
# случае на десятую часть, и их выравнивает перенос нескольких заказов к
# соседу. Развёртка по полярному углу вокруг депо укладывается всегда.
CAPACITY_SLACK = 0.2

# Окраина для сценария edge: здание, дальше которого от центра района лежит
# десятая часть зданий. Самые крайние не годятся, у границы выгрузки сеть
# обрезана.
EDGE_QUANTILE = 0.9


# --------------------------------------------------------------------------
# Разбор адреса
# --------------------------------------------------------------------------

def split_street(street):
    """Разделяет название улицы на родовое слово и собственно имя.

    «улица Ленина» -> ('улица', 'Ленина', 'before')
    «Ленинградский проспект» -> ('проспект', 'Ленинградский', 'after')
    «Ленинградское шоссе» -> ('шоссе', 'Ленинградское', 'after')
    Если родовое слово не распознано, возвращается (None, street, None).
    """
    words = street.split()
    if not words:
        return None, street, None
    lowered = [w.lower().strip(",.") for w in words]
    for kind in STREET_KINDS:
        if lowered[0] == kind:
            return kind, " ".join(words[1:]), "before"
        if lowered[-1] == kind:
            return kind, " ".join(words[:-1]), "after"
    return None, street, None


def decline_name(name):
    """Грубое склонение прилагательного в названии улицы.

    Полноценная морфология здесь не нужна: достаточно, чтобы точный поиск
    подстроки перестал находить название, а нечёткий продолжал.
    """
    if name.endswith("ая"):
        return name[:-2] + "ой"
    if name.endswith("ый") or name.endswith("ий"):
        return name[:-2] + "ом"
    if name.endswith("ое") or name.endswith("ее"):
        return name[:-2] + "ом"
    return name


# --------------------------------------------------------------------------
# Преобразования шума
# --------------------------------------------------------------------------

def noise_format(rng, parts):
    """Сокращения родового слова, префикс дома, перестановка частей."""
    kind, name, position = parts["kind"], parts["name"], parts["position"]
    house = parts["housenumber"]

    if kind and rng.random() < 0.7:
        kind_text = rng.choice(STREET_KINDS[kind]["short"])
    else:
        kind_text = kind

    if kind_text is None:
        street_text = name
    elif position == "after":
        street_text = "%s %s" % (name, kind_text)
    else:
        street_text = "%s %s" % (kind_text, name)

    house_text = rng.choice(HOUSE_PREFIXES) + house

    if rng.random() < 0.25:
        return "%s %s" % (house_text, street_text)
    separator = ", " if rng.random() < 0.8 else " "
    return "%s%s%s" % (street_text, separator, house_text)


def noise_morph(rng, parts):
    """Склонение: «на улице Ленина», «с Ленинградской, 5»."""
    kind, name, position = parts["kind"], parts["name"], parts["position"]
    house = parts["housenumber"]

    preposition, case = rng.choice([("на", "prep"), ("с", "gen"), ("по", "prep")])
    declined_name = decline_name(name)

    if kind:
        kind_text = STREET_KINDS[kind][case]
        if position == "after":
            street_text = "%s %s" % (declined_name, kind_text)
        else:
            street_text = "%s %s" % (kind_text, declined_name)
    else:
        street_text = declined_name

    return "%s %s, %s" % (preposition, street_text, house)


def noise_typo(rng, text):
    """Одна или две опечатки: вставка, удаление, замена, перестановка."""
    for _ in range(rng.randint(1, 2)):
        letters = [i for i, ch in enumerate(text) if ch.isalpha()]
        if not letters:
            break
        position = rng.choice(letters)
        operation = rng.choice(["substitute", "delete", "insert", "transpose"])
        char = text[position]
        lowered = char.lower()

        if operation == "substitute":
            neighbours = KEYBOARD_NEIGHBOURS.get(lowered)
            replacement = rng.choice(neighbours) if neighbours else lowered
            if char.isupper():
                replacement = replacement.upper()
            text = text[:position] + replacement + text[position + 1:]
        elif operation == "delete":
            text = text[:position] + text[position + 1:]
        elif operation == "insert":
            neighbours = KEYBOARD_NEIGHBOURS.get(lowered, lowered)
            text = text[:position] + rng.choice(neighbours) + text[position:]
        else:
            if position + 1 < len(text):
                text = text[:position] + text[position + 1] + text[position] + text[position + 2:]
    return text


def noise_case_space(rng, text):
    """Регистр, пробелы, пунктуация, ё и е."""
    choice = rng.random()
    if choice < 0.2:
        text = text.upper()
    elif choice < 0.35:
        text = text.lower()
    elif choice < 0.45:
        text = text.title()

    if rng.random() < 0.5:
        text = text.replace("ё", "е").replace("Ё", "Е")
    elif rng.random() < 0.2:
        text = text.replace("е", "ё", 1)

    if rng.random() < 0.4:
        text = text.replace(",", "")
    if rng.random() < 0.3:
        position = rng.randint(0, max(0, len(text) - 1))
        text = text[:position] + "  " + text[position:]
    if rng.random() < 0.2:
        text = " " + text + " "
    return text


def noise_homoglyph(rng, text):
    """Латинские двойники кириллических букв."""
    candidates = [i for i, ch in enumerate(text) if ch.lower() in HOMOGLYPHS]
    if not candidates:
        return text
    rng.shuffle(candidates)
    for position in candidates[: rng.randint(1, 2)]:
        char = text[position]
        replacement = HOMOGLYPHS[char.lower()]
        if char.isupper():
            replacement = replacement.upper()
        text = text[:position] + replacement + text[position + 1:]
    return text


def noise_extra_text(rng, text):
    """Комментарий курьеру вокруг адреса. Временное окно возвращается отдельно."""
    window = None
    pieces = [text]

    if rng.random() < 0.35:
        pieces.insert(0, rng.choice(COMMENT_PREFIXES))

    if rng.random() < 0.75:
        suffix = rng.choice(COMMENT_SUFFIXES)
        pieces.append(
            suffix.format(
                code=str(rng.randint(10, 99)) + rng.choice("КВБАкв"),
                entrance=rng.randint(1, 8),
                floor=rng.randint(1, 25),
                flat=rng.randint(1, 300),
                minutes=rng.choice([5, 10, 15, 20, 30]),
            )
        )

    if rng.random() < 0.55:
        start = rng.randint(8, 18)
        end = min(23, start + rng.randint(2, 6))
        pieces.append(rng.choice(TIME_PHRASES).format(a=start, b=end))
        window = (start, end)

    return ", ".join(pieces), window


# --------------------------------------------------------------------------
# Сборка заказа
# --------------------------------------------------------------------------

def make_order_text(rng, street, housenumber, classes):
    """Собирает текст заказа, применяя выбранные классы шума по порядку."""
    kind, name, position = split_street(street)
    parts = {"kind": kind, "name": name, "position": position, "housenumber": housenumber}

    if "morph" in classes:
        text = noise_morph(rng, parts)
    elif "format" in classes:
        text = noise_format(rng, parts)
    else:
        text = "%s, %s" % (street, housenumber)

    window = None
    if "extra_text" in classes:
        text, window = noise_extra_text(rng, text)
    if "typo" in classes:
        text = noise_typo(rng, text)
    if "homoglyph" in classes:
        text = noise_homoglyph(rng, text)
    if "case_space" in classes:
        text = noise_case_space(rng, text)

    return text, window


def pick_classes(rng):
    """От одного до трёх классов шума, без повторов."""
    names = list(NOISE_CLASSES)
    weights = [NOISE_WEIGHTS[n] for n in names]
    count = rng.choices([1, 2, 3], weights=[3, 5, 2])[0]
    chosen = []
    for _ in range(count):
        pick = rng.choices(names, weights=weights)[0]
        index = names.index(pick)
        names.pop(index)
        weights.pop(index)
        chosen.append(pick)
        if not names:
            break
    # «format» и «morph» описывают одно и то же место в строке, поэтому
    # morph поглощает format.
    if "morph" in chosen and "format" in chosen:
        chosen.remove("format")
    return sorted(chosen)


def make_missing_address(rng, foreign_streets, streets_present, house_numbers_by_street):
    """Адрес, которого в районе нет. Возвращает (строка улицы, номер дома, вид)."""
    foreign = [s for s in foreign_streets if s not in streets_present]
    if foreign and rng.random() < 0.4:
        street = rng.choice(foreign)
        return street, str(rng.randint(1, 60)), "foreign_street"

    # Номер берётся чаще из пропусков внутри диапазона улицы, а не только за
    # её последним домом: иначе отсев сводится к сравнению с максимумом.
    # Занятым считается и номер, с которого начинается корпус или строение
    # («12к2» занимает 12), чтобы ответ «такого дома нет» был бесспорным.
    street = rng.choice(sorted(streets_present))
    taken = set()
    for number in house_numbers_by_street[street]:
        digits = ""
        for ch in number:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            taken.add(int(digits))
    top = max(taken) if taken else 20
    gaps = [n for n in range(1, top + 1) if n not in taken]
    if gaps and rng.random() < 0.7:
        candidate = rng.choice(gaps)
    else:
        candidate = top + rng.randint(1, 6)
    return street, str(candidate), "missing_house"


# --------------------------------------------------------------------------
# Сценарий доставки
# --------------------------------------------------------------------------

def metres(lat1, lon1, lat2, lon2):
    """Расстояние в метрах в равнопромежуточной проекции. Для выбора депо в
    пределах района точности хватает с запасом."""
    k = 111320.0
    dx = (lon2 - lon1) * k * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * k
    return math.hypot(dx, dy)


def read_graph(district_dir):
    nodes_path = os.path.join(district_dir, "nodes.csv")
    edges_path = os.path.join(district_dir, "edges.csv")
    for path in (nodes_path, edges_path):
        if not os.path.exists(path):
            sys.exit(
                "Не найден %s.\n"
                "Генератору нужна дорожная сеть, чтобы поставить депо. "
                "Выгрузите район скриптом extract_district.py." % path
            )
    nodes = {}
    with open(nodes_path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            nodes[int(row["node_id"])] = (float(row["lat"]), float(row["lon"]))
    forward = {n: [] for n in nodes}
    backward = {n: [] for n in nodes}
    with open(edges_path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            u, v = int(row["from_id"]), int(row["to_id"])
            if u in nodes and v in nodes:
                forward[u].append(v)
                backward[v].append(u)
    return nodes, forward, backward


def largest_strong_members(nodes, forward, backward):
    """Вершины наибольшей сильно связной компоненты, алгоритм Косарайю."""
    order = []
    visited = set()
    for start in sorted(nodes):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(forward[start]))]
        while stack:
            node, neighbours = stack[-1]
            advanced = False
            for nxt in neighbours:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(forward[nxt])))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()

    seen = set()
    best = set()
    for node in reversed(order):
        if node in seen:
            continue
        component = {node}
        seen.add(node)
        queue = [node]
        while queue:
            current = queue.pop()
            for prev in backward[current]:
                if prev not in seen:
                    seen.add(prev)
                    component.add(prev)
                    queue.append(prev)
        if len(component) > len(best):
            best = component
    return best


def read_building_centres(district_dir):
    """Среднее точек контура каждого здания. Для выбора депо этого хватает:
    точка нужна, чтобы найти узлы сети в радиусе, а не чтобы привязать заказ."""
    path = os.path.join(district_dir, "building_points.csv")
    if not os.path.exists(path):
        sys.exit("Не найден %s. Выгрузите район скриптом extract_district.py." % path)
    sums = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            lat, lon = float(row["lat"]), float(row["lon"])
            acc = sums.setdefault(row["building_id"], [0.0, 0.0, 0])
            acc[0] += lat
            acc[1] += lon
            acc[2] += 1
    return {bid: (acc[0] / acc[2], acc[1] / acc[2]) for bid, acc in sums.items()}


def choose_depot(placement, buildings, centres, nodes, strong):
    """Здание для депо: в центре района или на окраине, с хорошей связностью
    вокруг. Выбор зависит только от района и сценария, но не от seed."""
    located = [(bid, centres[bid]) for bid, _street, _house in buildings if bid in centres]
    if not located:
        sys.exit("Ни у одного здания с адресом нет контура, депо поставить некуда.")

    lats = sorted(p[0] for _bid, p in located)
    lons = sorted(p[1] for _bid, p in located)
    centre = (lats[len(lats) // 2], lons[len(lons) // 2])
    by_distance = sorted(
        located, key=lambda item: (metres(centre[0], centre[1], item[1][0], item[1][1]), item[0])
    )

    if placement == "center":
        candidates = by_distance
    else:
        start = int(EDGE_QUANTILE * (len(by_distance) - 1))
        candidates = by_distance[start:] + by_distance[:start][::-1]

    # Сетка с клеткой в радиус: соседи точки ищутся в девяти клетках.
    cell_lat = DEPOT_RADIUS_M / 111320.0
    cell_lon = cell_lat / math.cos(math.radians(centre[0]))
    grid = {}
    for node_id, (lat, lon) in nodes.items():
        grid.setdefault((int(lat // cell_lat), int(lon // cell_lon)), []).append(node_id)

    for bid, (lat, lon) in candidates:
        row, col = int(lat // cell_lat), int(lon // cell_lon)
        near = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                for node_id in grid.get((row + dr, col + dc), ()):
                    nlat, nlon = nodes[node_id]
                    if metres(lat, lon, nlat, nlon) <= DEPOT_RADIUS_M:
                        near.append(node_id)
        if near and all(node_id in strong for node_id in near):
            return bid
    sys.exit(
        "Не нашлось здания для депо: рядом с каждым есть узлы вне наибольшей "
        "сильно связной компоненты. Проверьте выгрузку района."
    )


def courier_capacity(weights, couriers):
    """Вместимость с запасом в CAPACITY_SLACK над средней загрузкой, но не
    меньше средней загрузки плюс самый тяжёлый заказ.

    Нижняя граница гарантирует, что раскладка существует. Если раскладывать
    заказы по одному в любом порядке и закрывать курьера, когда следующий
    заказ не влезает, каждый закрытый курьер загружен больше чем на
    (вместимость минус заказ), то есть больше средней загрузки, поэтому
    курьеров хватает. Свойство сохраняется для любого подмножества заказов,
    в том числе после отсева несуществующих адресов."""
    average = sum(weights) / couriers
    return max(math.ceil(average * (1 + CAPACITY_SLACK)), math.ceil(average) + math.ceil(max(weights)))


def read_foreign_streets(path):
    with open(path, encoding="utf-8") as handle:
        streets = [line.strip() for line in handle]
    streets = [s for s in streets if s and not s.startswith("#")]
    if not streets:
        sys.exit("Файл %s не содержит ни одной улицы." % path)
    return streets


# --------------------------------------------------------------------------

def read_buildings(district_dir):
    path = os.path.join(district_dir, "buildings.csv")
    if not os.path.exists(path):
        sys.exit(
            "Не найден %s.\n"
            "Сначала выгрузите район скриптом extract_district.py." % path
        )
    buildings = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            street = (row.get("street") or "").strip()
            house = (row.get("housenumber") or "").strip()
            if street and house:
                buildings.append((row["building_id"], street, house))
    buildings.sort(key=lambda item: item[0])
    return buildings


def main():
    parser = argparse.ArgumentParser(
        description="Генератор потока заказов в свободной форме для итогового проекта."
    )
    parser.add_argument("--district", required=True, help="каталог с выгрузкой района")
    parser.add_argument("--seed", type=int, required=True, help="значение seed, задаёт весь поток")
    parser.add_argument("--count", type=int, default=200, help="число заказов")
    parser.add_argument(
        "--missing-rate",
        type=float,
        default=0.07,
        help="доля заказов с несуществующим адресом, от 0 до 1",
    )
    parser.add_argument(
        "--variant",
        choices=sorted(VARIANTS),
        default="base",
        help="сценарий доставки: где депо, сколько курьеров, ограничена ли вместимость",
    )
    parser.add_argument(
        "--foreign-streets",
        help="файл со списком улиц для заказов с несуществующим адресом, по одной в строке; "
        "без него берётся встроенный список",
    )
    parser.add_argument("--out", required=True, help="каталог для результата")
    parser.add_argument(
        "--with-truth",
        action="store_true",
        help="записать разметку truth.csv; без флага правильные ответы не сохраняются",
    )
    args = parser.parse_args()

    if not 0 <= args.missing_rate < 1:
        sys.exit("Доля заказов с несуществующим адресом должна лежать в промежутке от 0 до 1.")
    if args.count < 1:
        sys.exit("Число заказов должно быть положительным.")

    variant = VARIANTS[args.variant]
    foreign_streets = (
        read_foreign_streets(args.foreign_streets) if args.foreign_streets else FOREIGN_STREETS
    )

    buildings = read_buildings(args.district)
    if len(buildings) < 50:
        sys.exit("В районе всего %d зданий с адресом, этого мало для потока заказов." % len(buildings))

    streets_present = {street for _bid, street, _house in buildings}
    house_numbers_by_street = {}
    for _bid, street, house in buildings:
        house_numbers_by_street.setdefault(street, set()).add(house)

    nodes, forward, backward = read_graph(args.district)
    strong = largest_strong_members(nodes, forward, backward)
    centres = read_building_centres(args.district)
    depot_id = choose_depot(variant["depot"], buildings, centres, nodes, strong)

    # Депо не бывает адресом заказа.
    order_buildings = [b for b in buildings if b[0] != depot_id]

    rng = random.Random(args.seed)

    missing_count = int(round(args.count * args.missing_rate))
    real_count = args.count - missing_count

    if real_count <= len(order_buildings):
        chosen = rng.sample(order_buildings, real_count)
    else:
        chosen = [rng.choice(order_buildings) for _ in range(real_count)]

    orders = []
    class_counter = {name: 0 for name in NOISE_CLASSES}

    for building_id, street, house in chosen:
        classes = pick_classes(rng)
        text, window = make_order_text(rng, street, house, classes)
        for name in classes:
            class_counter[name] += 1
        orders.append(
            {
                "building_id": building_id,
                "street": street,
                "housenumber": house,
                "text": text,
                "classes": classes,
                "window": window,
                "weight": round(rng.uniform(0.3, 15.0), 1),
            }
        )

    missing_kinds = {"foreign_street": 0, "missing_house": 0}
    for _ in range(missing_count):
        street, house, kind = make_missing_address(
            rng, foreign_streets, streets_present, house_numbers_by_street
        )
        missing_kinds[kind] += 1
        classes = pick_classes(rng)
        text, window = make_order_text(rng, street, house, classes)
        for name in classes:
            class_counter[name] += 1
        orders.append(
            {
                "building_id": "",
                "street": street,
                "housenumber": house,
                "text": text,
                "classes": classes + [kind],
                "window": window,
                "weight": round(rng.uniform(0.3, 15.0), 1),
            }
        )

    rng.shuffle(orders)

    capacity = None
    if variant["capacity"]:
        capacity = courier_capacity([o["weight"] for o in orders], variant["couriers"])
    scenario = {
        "variant": args.variant,
        "depot_building_id": depot_id,
        "couriers": variant["couriers"],
        "capacity_kg": capacity,
    }

    os.makedirs(args.out, exist_ok=True)

    with open(os.path.join(args.out, "scenario.json"), "w", encoding="utf-8") as handle:
        json.dump(scenario, handle, ensure_ascii=False, indent=2, sort_keys=True)

    with open(os.path.join(args.out, "orders.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_id", "text", "weight_kg"])
        for index, order in enumerate(orders, start=1):
            writer.writerow(["o%04d" % index, order["text"], order["weight"]])

    if args.with_truth:
        with open(os.path.join(args.out, "truth.csv"), "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["order_id", "building_id", "street", "housenumber", "noise_classes", "time_from", "time_to"]
            )
            for index, order in enumerate(orders, start=1):
                window = order["window"]
                writer.writerow(
                    [
                        "o%04d" % index,
                        order["building_id"],
                        order["street"],
                        order["housenumber"],
                        " ".join(order["classes"]),
                        "" if window is None else "%02d:00" % window[0],
                        "" if window is None else "%02d:00" % window[1],
                    ]
                )

    run = {
        "generator_version": GENERATOR_VERSION,
        "district": os.path.basename(os.path.normpath(args.district)),
        "seed": args.seed,
        "variant": args.variant,
        "variant_about": variant["about"],
        "scenario": scenario,
        "foreign_streets": "file" if args.foreign_streets else "builtin",
        "count": args.count,
        "missing_rate": args.missing_rate,
        "missing_orders": missing_count,
        "missing_kinds": missing_kinds,
        "orders_with_time_window": sum(1 for o in orders if o["window"] is not None),
        "noise_class_counts": class_counter,
        "noise_weights": NOISE_WEIGHTS,
        "truth_written": bool(args.with_truth),
    }
    with open(os.path.join(args.out, "run.json"), "w", encoding="utf-8") as handle:
        json.dump(run, handle, ensure_ascii=False, indent=2, sort_keys=True)

    print("Каталог: %s" % args.out)
    print("Сценарий %s: %s" % (args.variant, variant["about"]))
    print(
        "Депо: здание %s, курьеров: %d, вместимость: %s"
        % (depot_id, variant["couriers"], "не ограничена" if capacity is None else "%d кг" % capacity)
    )
    print("Заказов: %d, из них с несуществующим адресом: %d" % (args.count, missing_count))
    print("С временным окном в тексте: %d" % run["orders_with_time_window"])
    print("Классы шума: " + ", ".join("%s %d" % (k, v) for k, v in sorted(class_counter.items())))
    if args.with_truth:
        print("Разметка записана в truth.csv")
    else:
        print("Разметка не записана. Для неё нужен флаг --with-truth")


if __name__ == "__main__":
    main()
