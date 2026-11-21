#!/usr/bin/env python3
"""Выгрузка района из OpenStreetMap в простые текстовые файлы.

Готовит данные для итогового проекта: дорожную сеть и здания с адресами.
Формат намеренно примитивный, чтобы ядро на C, Go, Java или Python читало
его без разбора OSM.

Два способа получить данные:

  1. Через OSMnx, по названию места или по прямоугольнику координат.
     Нужен установленный osmnx, работает через Overpass API.

       python3 extract_district.py --place "Академический район, Москва" --out data/akademichesky
       python3 extract_district.py --bbox 37.55,55.68,37.60,55.71 --out data/bbox

  2. Из файла OSM XML, без сторонних библиотек. Такой файл отдаёт BBBike,
     либо его можно получить из .osm.pbf с Geofabrik утилитой osmium:

       osmium extract -b 37.55,55.68,37.60,55.71 central-fed-district.osm.pbf -o district.osm
       python3 extract_district.py --osm-xml district.osm --out data/district

Что получается на выходе (см. README.md рядом со скриптом):

  nodes.csv                   вершины дорожной сети
  edges.csv                   рёбра, уже с учётом односторонних дорог
  buildings.csv               здания с адресами
  building_points.csv         контуры зданий
  streets.csv                 справочник улиц
  meta.json                   параметры выгрузки, статистика, проверка порогов
  reference/edge_lengths.csv  длины рёбер для сверки со своей реализацией

Длины рёбер в edges.csv намеренно отсутствуют: их вы считаете сами из
координат. Файл reference/edge_lengths.csv нужен только для самопроверки.

Данные OpenStreetMap распространяются по лицензии ODbL. В отчёте нужна
строка «© OpenStreetMap contributors».
"""

import argparse
import bz2
import csv
import gzip
import json
import math
import os
import sys
import xml.etree.ElementTree as ET

# Пороги пригодности района. Совпадают с приложением к заданию.
MIN_NODES = 3000
MIN_BUILDINGS = 1000
MIN_STRONG_COMPONENT = 0.90

# Типы дорог, по которым может проехать курьер на автомобиле.
DRIVE_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service", "road",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
}

# Служебные проезды, которые дорогой общего пользования не являются.
EXCLUDED_SERVICE = {"parking_aisle", "driveway", "emergency_access", "drive-through"}

# Эти типы дорог односторонние по умолчанию, без явного тега.
IMPLICIT_ONEWAY_HIGHWAYS = {"motorway", "motorway_link"}

EARTH_RADIUS_M = 6371008.8


def haversine(lat1, lon1, lat2, lon2):
    """Расстояние между двумя точками по большому кругу, в метрах."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------
# Разбор OSM XML
# --------------------------------------------------------------------------

def open_maybe_compressed(path):
    if path.endswith(".bz2"):
        return bz2.open(path, "rb")
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def tags_of(elem):
    return {t.get("k"): t.get("v") for t in elem.findall("tag")}


def is_drive_road(tags):
    highway = tags.get("highway")
    if highway not in DRIVE_HIGHWAYS:
        return False
    if tags.get("service") in EXCLUDED_SERVICE:
        return False
    if tags.get("access") in ("no", "private"):
        return False
    if tags.get("area") == "yes":
        return False
    return True


def oneway_direction(tags):
    """Возвращает 1 для прямого направления, -1 для обратного, 0 для двустороннего."""
    value = (tags.get("oneway") or "").strip().lower()
    if value in ("yes", "true", "1"):
        return 1
    if value in ("-1", "reverse"):
        return -1
    if value in ("no", "false", "0"):
        return 0
    if tags.get("junction") in ("roundabout", "circular"):
        return 1
    if tags.get("highway") in IMPLICIT_ONEWAY_HIGHWAYS:
        return 1
    return 0


def parse_osm_xml(path, bbox=None):
    """Читает OSM XML и возвращает сырые узлы, дороги и здания.

    bbox, если задан, отсекает всё, что вне прямоугольника (west, south, east, north).
    """
    coords = {}
    roads = []
    building_ways = []
    building_nodes = []

    with open_maybe_compressed(path) as handle:
        for event, elem in ET.iterparse(handle, events=("end",)):
            tag = elem.tag
            if tag == "node":
                node_id = int(elem.get("id"))
                lat = float(elem.get("lat"))
                lon = float(elem.get("lon"))
                coords[node_id] = (lat, lon)
                tags = tags_of(elem)
                if tags.get("addr:street") and tags.get("addr:housenumber"):
                    building_nodes.append((node_id, tags))
            elif tag == "way":
                way_id = int(elem.get("id"))
                refs = [int(nd.get("ref")) for nd in elem.findall("nd")]
                tags = tags_of(elem)
                if is_drive_road(tags) and len(refs) >= 2:
                    roads.append((way_id, refs, tags))
                elif tags.get("building") and tags.get("addr:street") and tags.get("addr:housenumber"):
                    building_ways.append((way_id, refs, tags))
            if tag in ("node", "way", "relation"):
                elem.clear()

    if bbox is not None:
        west, south, east, north = bbox
        coords = {
            nid: (lat, lon)
            for nid, (lat, lon) in coords.items()
            if south <= lat <= north and west <= lon <= east
        }

    return coords, roads, building_ways, building_nodes


def build_graph_from_roads(coords, roads):
    """Собирает список рёбер из дорог, разбивая каждую на отрезки между узлами."""
    nodes = {}
    edges = {}

    for _way_id, refs, tags in roads:
        direction = oneway_direction(tags)
        highway = tags.get("highway", "")
        maxspeed = tags.get("maxspeed", "")
        for a, b in zip(refs, refs[1:]):
            if a not in coords or b not in coords or a == b:
                continue
            nodes[a] = coords[a]
            nodes[b] = coords[b]
            if direction >= 0:
                edges.setdefault((a, b), (highway, maxspeed))
            if direction <= 0:
                edges.setdefault((b, a), (highway, maxspeed))

    return nodes, edges


# Точка с адресом, у которой есть один из этих тегов, — заведение или вход
# внутри здания, а не само здание. В справочник она не попадает, иначе у
# одного адреса окажется несколько объектов.
POI_KEYS = {"shop", "amenity", "office", "craft", "tourism", "leisure", "healthcare", "entrance"}


def buildings_from_xml(coords, building_ways, building_nodes):
    """Приводит здания к единому виду: идентификатор, адрес, контур из точек.

    Здание, часть контура которого отрезана границей выгрузки, пропускается:
    от обрезанного контура не построить ни точку, ни площадь. Адресная точка
    берётся, только если это не заведение и если такого адреса нет у контура.
    """
    buildings = []
    for way_id, refs, tags in building_ways:
        if any(r not in coords for r in refs):
            continue
        contour = [coords[r] for r in refs]
        if len(contour) >= 3:
            if contour[0] == contour[-1] and len(contour) > 3:
                contour = contour[:-1]
            buildings.append(("w%d" % way_id, tags["addr:street"], tags["addr:housenumber"], contour))
    with_contour = {(street, house) for _bid, street, house, _contour in buildings}
    for node_id, tags in building_nodes:
        if node_id not in coords or POI_KEYS & set(tags):
            continue
        address = (tags["addr:street"], tags["addr:housenumber"])
        if address in with_contour:
            continue
        with_contour.add(address)
        buildings.append(("n%d" % node_id, address[0], address[1], [coords[node_id]]))
    return buildings


# --------------------------------------------------------------------------
# Выгрузка через OSMnx
# --------------------------------------------------------------------------

def extract_with_osmnx(place=None, bbox=None):
    try:
        import osmnx as ox
    except ImportError:
        sys.exit(
            "Не найден osmnx. Установите его командой «pip install osmnx» либо\n"
            "воспользуйтесь режимом --osm-xml, которому сторонние библиотеки не нужны."
        )

    # simplify=False оставляет граф на уровне отдельных узлов дороги. Тогда
    # длина ребра равна расстоянию между его концами по прямой, и её можно
    # честно посчитать самому. retain_all=True сохраняет несвязные куски,
    # иначе доля наибольшей компоненты всегда была бы единицей.
    graph_args = dict(network_type="drive", simplify=False, retain_all=True)
    tags = {"building": True}

    if place:
        graph = ox.graph_from_place(place, **graph_args)
        features = ox.features_from_place(place, tags=tags)
    else:
        west, south, east, north = bbox
        try:
            graph = ox.graph_from_bbox(bbox=(west, south, east, north), **graph_args)
            features = ox.features_from_bbox(bbox=(west, south, east, north), tags=tags)
        except TypeError:
            # Совместимость с osmnx 1.x, где границы передавались по отдельности.
            graph = ox.graph_from_bbox(north=north, south=south, east=east, west=west, **graph_args)
            features = ox.features_from_bbox(north=north, south=south, east=east, west=west, tags=tags)

    nodes = {}
    for node_id, data in graph.nodes(data=True):
        nodes[int(node_id)] = (float(data["y"]), float(data["x"]))

    edges = {}
    for u, v, data in graph.edges(data=True):
        u, v = int(u), int(v)
        if u == v:
            continue
        highway = data.get("highway", "")
        if isinstance(highway, list):
            highway = highway[0] if highway else ""
        maxspeed = data.get("maxspeed", "")
        if isinstance(maxspeed, list):
            maxspeed = maxspeed[0] if maxspeed else ""
        edges.setdefault((u, v), (str(highway), str(maxspeed)))

    buildings = []
    for osmid, row in features.iterrows():
        street = row.get("addr:street")
        housenumber = row.get("addr:housenumber")
        if not isinstance(street, str) or not isinstance(housenumber, str):
            continue
        geometry = row.get("geometry")
        if geometry is None or geometry.is_empty:
            continue
        contour = contour_from_geometry(geometry)
        if not contour:
            continue
        element, ident = (osmid if isinstance(osmid, tuple) else ("way", osmid))
        buildings.append(("%s%s" % (str(element)[0], ident), street, housenumber, contour))

    return nodes, edges, buildings


def contour_from_geometry(geometry):
    """Достаёт из геометрии список точек контура в виде (широта, долгота)."""
    kind = geometry.geom_type
    if kind == "Point":
        return [(geometry.y, geometry.x)]
    if kind == "Polygon":
        return [(y, x) for x, y in geometry.exterior.coords][:-1]
    if kind == "MultiPolygon":
        largest = max(geometry.geoms, key=lambda g: g.area)
        return [(y, x) for x, y in largest.exterior.coords][:-1]
    if kind in ("LineString", "LinearRing"):
        return [(y, x) for x, y in geometry.coords]
    return []


# --------------------------------------------------------------------------
# Связность
# --------------------------------------------------------------------------

def adjacency(nodes, edges):
    forward = {n: [] for n in nodes}
    backward = {n: [] for n in nodes}
    for (u, v) in edges:
        forward[u].append(v)
        backward[v].append(u)
    return forward, backward


def largest_strong_component(nodes, forward, backward):
    """Размер наибольшей сильно связной компоненты, алгоритм Косарайю."""
    order = []
    visited = set()
    for start in nodes:
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
    best = 0
    for node in reversed(order):
        if node in seen:
            continue
        size = 0
        queue = [node]
        seen.add(node)
        while queue:
            current = queue.pop()
            size += 1
            for prev in backward[current]:
                if prev not in seen:
                    seen.add(prev)
                    queue.append(prev)
        best = max(best, size)
    return best


def largest_weak_component(nodes, forward, backward):
    """Размер наибольшей компоненты, если забыть про направление рёбер."""
    seen = set()
    best = 0
    for start in nodes:
        if start in seen:
            continue
        size = 0
        queue = [start]
        seen.add(start)
        while queue:
            current = queue.pop()
            size += 1
            for nxt in forward[current] + backward[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        best = max(best, size)
    return best


# --------------------------------------------------------------------------
# Запись результата
# --------------------------------------------------------------------------

def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_outputs(outdir, nodes, edges, buildings, meta):
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(os.path.join(outdir, "reference"), exist_ok=True)

    node_ids = sorted(nodes)
    write_csv(
        os.path.join(outdir, "nodes.csv"),
        ["node_id", "lat", "lon"],
        [(nid, "%.7f" % nodes[nid][0], "%.7f" % nodes[nid][1]) for nid in node_ids],
    )

    edge_keys = sorted(edges)
    write_csv(
        os.path.join(outdir, "edges.csv"),
        ["from_id", "to_id", "highway", "maxspeed"],
        [(u, v, edges[(u, v)][0], edges[(u, v)][1]) for (u, v) in edge_keys],
    )

    write_csv(
        os.path.join(outdir, "reference", "edge_lengths.csv"),
        ["from_id", "to_id", "length_m"],
        [
            (u, v, "%.3f" % haversine(nodes[u][0], nodes[u][1], nodes[v][0], nodes[v][1]))
            for (u, v) in edge_keys
        ],
    )

    buildings = sorted(buildings, key=lambda b: b[0])
    write_csv(
        os.path.join(outdir, "buildings.csv"),
        ["building_id", "street", "housenumber", "n_points"],
        [(bid, street, house, len(contour)) for bid, street, house, contour in buildings],
    )

    contour_rows = []
    for bid, _street, _house, contour in buildings:
        for seq, (lat, lon) in enumerate(contour):
            contour_rows.append((bid, seq, "%.7f" % lat, "%.7f" % lon))
    write_csv(
        os.path.join(outdir, "building_points.csv"),
        ["building_id", "seq", "lat", "lon"],
        contour_rows,
    )

    street_counts = {}
    for _bid, street, _house, _contour in buildings:
        street_counts[street] = street_counts.get(street, 0) + 1
    write_csv(
        os.path.join(outdir, "streets.csv"),
        ["street", "buildings_count"],
        sorted(street_counts.items(), key=lambda item: (-item[1], item[0])),
    )

    with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------------------

def parse_bbox(text):
    parts = [float(x) for x in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("Ожидается west,south,east,north")
    west, south, east, north = parts
    if west >= east or south >= north:
        raise argparse.ArgumentTypeError("Порядок значений: west,south,east,north")
    return (west, south, east, north)


def main():
    parser = argparse.ArgumentParser(
        description="Выгрузка района OpenStreetMap в текстовые файлы для итогового проекта.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--place", help="название района, например «Академический район, Москва»")
    source.add_argument("--bbox", type=parse_bbox, help="прямоугольник west,south,east,north")
    source.add_argument("--osm-xml", help="путь к файлу OSM XML, можно .bz2 или .gz")
    parser.add_argument("--clip-bbox", type=parse_bbox, help="обрезать данные из --osm-xml прямоугольником")
    parser.add_argument("--out", required=True, help="каталог для результата")
    parser.add_argument("--force", action="store_true", help="не завершаться с ошибкой при непройденных порогах")
    args = parser.parse_args()

    if args.osm_xml:
        source_description = {"kind": "osm-xml", "path": os.path.basename(args.osm_xml)}
        if args.clip_bbox:
            source_description["clip_bbox"] = list(args.clip_bbox)
        coords, roads, building_ways, building_nodes = parse_osm_xml(args.osm_xml, args.clip_bbox)
        nodes, edges = build_graph_from_roads(coords, roads)
        buildings = buildings_from_xml(coords, building_ways, building_nodes)
    else:
        source_description = {"kind": "osmnx"}
        if args.place:
            source_description["place"] = args.place
        else:
            source_description["bbox"] = list(args.bbox)
        nodes, edges, buildings = extract_with_osmnx(args.place, args.bbox)

    if not nodes:
        sys.exit("В выгрузке нет ни одной вершины дорожной сети. Проверьте границы района.")

    forward, backward = adjacency(nodes, edges)
    strong = largest_strong_component(nodes, forward, backward)
    weak = largest_weak_component(nodes, forward, backward)
    strong_share = strong / len(nodes)
    weak_share = weak / len(nodes)

    checks = {
        "nodes": {"value": len(nodes), "threshold": MIN_NODES, "passed": len(nodes) >= MIN_NODES},
        "buildings": {
            "value": len(buildings),
            "threshold": MIN_BUILDINGS,
            "passed": len(buildings) >= MIN_BUILDINGS,
        },
        "strong_component_share": {
            "value": round(strong_share, 4),
            "threshold": MIN_STRONG_COMPONENT,
            "passed": strong_share >= MIN_STRONG_COMPONENT,
        },
    }

    meta = {
        "source": source_description,
        "attribution": "© OpenStreetMap contributors, ODbL",
        "nodes": len(nodes),
        "edges": len(edges),
        "buildings_with_address": len(buildings),
        "streets": len({b[1] for b in buildings}),
        "largest_strong_component": strong,
        "largest_strong_component_share": round(strong_share, 4),
        "largest_weak_component": weak,
        "largest_weak_component_share": round(weak_share, 4),
        "thresholds_passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
    }

    write_outputs(args.out, nodes, edges, buildings, meta)

    print("Каталог: %s" % args.out)
    print("Вершин дорожной сети: %d" % len(nodes))
    print("Рёбер (с учётом направлений): %d" % len(edges))
    print("Зданий с полным адресом: %d" % len(buildings))
    print("Улиц в справочнике: %d" % meta["streets"])
    print("Наибольшая сильно связная компонента: %d вершин (%.1f%%)" % (strong, 100 * strong_share))
    print("Наибольшая слабо связная компонента: %d вершин (%.1f%%)" % (weak, 100 * weak_share))

    if not meta["thresholds_passed"]:
        print("\nПороги пригодности не пройдены:")
        for name, check in sorted(checks.items()):
            if not check["passed"]:
                print("  %s: %s при пороге %s" % (name, check["value"], check["threshold"]))
        print("Возьмите район большего размера или обратитесь на консультации.")
        if not args.force:
            sys.exit(1)
    else:
        print("\nПороги пригодности пройдены.")


if __name__ == "__main__":
    main()
