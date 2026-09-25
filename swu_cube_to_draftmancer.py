#!/usr/bin/env python3
"""
SWU Cube -> Draftmancer

Convertit un export de cube Star Wars Unlimited (JSON) en liste de cartes
personnalisées au format Draftmancer.

--- Statut actuel du projet ---
Étape 2/N : récupération des cartes auprès de l'API SWUDB (avec cache local)
et vérification que chaque carte du cube est bien retrouvée.
On ne génère pas encore le fichier Draftmancer final, c'est la prochaine étape.

Usage :
    python swu_cube_to_draftmancer.py mon_cube.json
    python swu_cube_to_draftmancer.py mon_cube.json --only-set ASH   (pour tester sur un seul set)
    python swu_cube_to_draftmancer.py mon_cube.json --refresh-cache  (pour forcer un nouveau téléchargement)
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "https://api.swu-db.com"

# Les 4 sections attendues dans un export de cube SWU.
SECTIONS = ["leader", "base", "deck", "sideboard"]


def load_cube(path: Path) -> dict:
    """Charge le fichier JSON du cube en forçant l'UTF-8."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Erreur : le fichier '{path}' est introuvable.")
    except json.JSONDecodeError as e:
        sys.exit(f"Erreur : '{path}' n'est pas un JSON valide ({e}).")


def parse_card_id(card_id: str) -> tuple[str, str]:
    """
    Découpe un id de carte du type 'ASH_009' en (set, number) -> ('ASH', '009').
    C'est ce (set, number) qui nous servira plus tard à retrouver la carte
    dans les données récupérées auprès de l'API SWUDB.
    """
    if "_" not in card_id:
        sys.exit(f"Erreur : id de carte inattendu (pas de '_') : '{card_id}'")
    set_code, number = card_id.split("_", 1)
    return set_code.upper(), number


def extract_entries(cube: dict) -> list[dict]:
    """
    Transforme le JSON du cube en une liste plate d'entrées, une par carte :
    {"section": "leader", "id": "ASH_009", "set": "ASH", "number": "009", "count": 1}
    """
    entries = []
    for section in SECTIONS:
        if section not in cube:
            print(f"Attention : la section '{section}' est absente du fichier.")
            continue
        for card in cube[section]:
            set_code, number = parse_card_id(card["id"])
            entries.append({
                "section": section,
                "id": card["id"],
                "set": set_code,
                "number": number,
                "count": card.get("count", 1),
            })
    return entries


def print_summary(entries: list[dict]) -> None:
    """Affiche un résumé lisible : nb de cartes par section, sets utilisés."""
    print("\n--- Résumé du cube ---")

    for section in SECTIONS:
        section_entries = [e for e in entries if e["section"] == section]
        total_copies = sum(e["count"] for e in section_entries)
        print(f"  {section:10s} : {len(section_entries)} carte(s) distincte(s), "
              f"{total_copies} exemplaire(s) au total")

    sets_used = sorted({e["set"] for e in entries})
    print(f"\n  Sets utilisés ({len(sets_used)}) : {', '.join(sets_used)}")
    print(f"  Total de cartes distinctes : {len(entries)}")


def fetch_set_cards(set_code: str) -> list[dict]:
    """Appelle l'API SWUDB pour récupérer toutes les cartes d'un set donné."""
    url = f"{API_BASE}/cards/{set_code.lower()}"
    req = Request(url, headers={"User-Agent": "swu-cube-to-draftmancer/0.1"})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = json.load(resp)
    except HTTPError as e:
        sys.exit(f"Erreur HTTP en récupérant le set '{set_code}' ({url}) : {e}")
    except URLError as e:
        sys.exit(f"Erreur réseau en récupérant le set '{set_code}' ({url}) : {e}")

    # La doc de l'API ne précise pas explicitement si la réponse est une liste
    # brute de cartes ou un objet avec une clé "data". On gère les deux cas,
    # et on affiche un message clair si le format ne correspond à aucun des
    # deux, pour faciliter le debug plutôt que de planter sans explication.
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("data"), list):
        return raw["data"]
    sys.exit(
        f"Format de réponse inattendu pour le set '{set_code}'. "
        f"Type reçu : {type(raw).__name__}. "
        f"Clés (si objet) : {list(raw.keys()) if isinstance(raw, dict) else 'N/A'}"
    )


def get_set_cards(set_code: str, cache_dir: Path, refresh: bool = False) -> list[dict]:
    """Renvoie les cartes d'un set, en utilisant le cache local si possible."""
    cache_file = cache_dir / f"{set_code.upper()}.json"
    if cache_file.exists() and not refresh:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    print(f"  Téléchargement du set {set_code}...")
    cards = fetch_set_cards(set_code)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    return cards


def normalize_number(number: str) -> str:
    """Normalise un numéro de carte pour la comparaison (ex: '002' et '2' -> '2')."""
    stripped = number.lstrip("0")
    return stripped if stripped else "0"


def build_card_index(cards_by_set: dict[str, list[dict]]) -> tuple[dict, dict]:
    """
    Construit deux index (set, number) -> carte : un exact, un avec numéro
    normalisé (sans zéros de tête) en secours.

    L'API renvoie plusieurs variantes (foil, Hyperspace, Showcase...) par
    carte, qui partagent le même (set, number). On préfère systématiquement
    la variante "Normal" pour ne pas récupérer une image de variante par
    accident.
    """
    index, index_normalized = {}, {}

    def maybe_store(target: dict, key: tuple, card: dict) -> None:
        existing = target.get(key)
        if existing is None:
            target[key] = card
        elif existing.get("VariantType") != "Normal" and card.get("VariantType") == "Normal":
            target[key] = card  # on remplace une variante par la version Normal

    for set_code, cards in cards_by_set.items():
        for card in cards:
            set_up = card["Set"].upper()
            number = card["Number"]
            maybe_store(index, (set_up, number), card)
            maybe_store(index_normalized, (set_up, normalize_number(number)), card)

    return index, index_normalized


def match_entries(
    entries: list[dict], index: dict, index_normalized: dict
) -> tuple[list[dict], list[dict]]:
    """Sépare les entrées du cube en 'trouvées' et 'manquantes', avec repli sur le numéro normalisé."""
    found, missing = [], []
    for entry in entries:
        key = (entry["set"], entry["number"])
        card = index.get(key)
        if card is None:
            card = index_normalized.get((entry["set"], normalize_number(entry["number"])))
        if card is None:
            missing.append(entry)
        else:
            found.append({**entry, "card": card})
    return found, missing


def diagnose_missing(missing: list[dict], cards_by_set: dict[str, list[dict]]) -> None:
    """Pour les sets encore problématiques, affiche les numéros réellement disponibles."""
    problem_sets = sorted({e["set"] for e in missing})
    for set_code in problem_sets:
        available = sorted({c["Number"] for c in cards_by_set.get(set_code, [])})
        wanted = sorted({e["number"] for e in missing if e["set"] == set_code})
        print(f"\n  Set '{set_code}' : numéro(s) cherché(s) {wanted}")
        print(f"    Numéros disponibles dans ce set ({len(available)}) : "
              f"{available[:15]}{' ...' if len(available) > 15 else ''}")


def card_display_name(card: dict) -> str:
    """
    Nom affiché de la carte. On inclut le sous-titre quand il existe car
    plusieurs leaders/cartes SWU partagent le même Name avec des Subtitle
    différents (ex: plusieurs versions d'Ahsoka Tano) -> sans ça on aurait
    des collisions de nom dans le CustomCards.
    """
    name = card["Name"]
    subtitle = card.get("Subtitle")
    type = card.get("Type")
    return f"{name} | {subtitle}" if subtitle and type != "Base" else name


def card_to_custom_card(card: dict) -> dict:
    """
    Convertit une carte SWUDB en dictionnaire CustomCard Draftmancer.
    Volontairement minimal : seuls name/type/image sont utiles ici (le
    reste - mana_cost, subtypes, rarity, set... - n'est pas exploité et
    peut même provoquer des erreurs de validation côté Draftmancer, comme
    "Legendary" qui n'est pas une rareté valide pour Draftmancer).
    Le verso (back) sera ajouté dans une prochaine étape.
    """
    return {
        "name": card_display_name(card),
        "mana_cost": "",  # champ obligatoire côté Draftmancer, mais inutilisé ici
        "type": card.get("Type", ""),
        "image": card.get("FrontArt", ""),
    }


def report_duplicate_names(found: list[dict]) -> None:
    """
    Avertit (sans bloquer) si plusieurs cartes différentes du cube finissent
    avec le même nom affiché. Comme le CustomCard ne porte plus set/numéro,
    Draftmancer traitera ces cartes comme identiques (même image, celle de
    la première occurrence) : c'est un choix assumé de simplicité, mais
    utile de savoir si/où ça arrive dans ton cube.
    """
    seen = {}
    duplicates = set()
    for f in found:
        name = card_display_name(f["card"])
        printing = (f["card"].get("Set"), f["card"].get("Number"))
        if name in seen and seen[name] != printing:
            duplicates.add(name)
        seen[name] = printing
    if duplicates:
        print(f"\n  Info : {len(duplicates)} nom(s) partagé(s) par plusieurs impressions différentes "
              f"(elles utiliseront toutes l'image de la première rencontrée) :")
        for name in sorted(duplicates)[:10]:
            print(f"    - {name}")


SLOT_ORDER = [
    ("Leader", 1),
    ("Base", 1),
    ("Bleu", 1),
    ("Rouge", 1),
    ("Jaune", 1),
    ("Vert", 1),
    ("Noir", 3),
    ("Blanc", 3),
    ("Wildcard", 4),
]


def classify_aspect(card: dict) -> str | None:
    """Catégorie couleur d'une carte non-Leader/Base, ou None si aucune ne correspond."""
    aspects = card.get("Aspects") or []
    if aspects == ["Vigilance"]:
        return "Bleu"
    if aspects == ["Aggression"]:
        return "Rouge"
    if aspects == ["Cunning"]:
        return "Jaune"
    if aspects == ["Command"]:
        return "Vert"
    if "Villainy" in aspects:
        return "Noir"
    if "Heroism" in aspects:
        return "Blanc"
    return None


def categorize_cards(found: list[dict]) -> dict[str, list[dict]]:
    """
    Répartit les cartes trouvées dans les sheets. "Wildcard" contient TOUTES
    les cartes hors Leader/Base (chevauchement volontaire avec les sheets
    colorées) : la protection contre les doublons dans un même booster est
    déléguée au réglage 'duplicateProtection' de Draftmancer.
    """
    categories = {name: [] for name, _ in SLOT_ORDER}
    for f in found:
        card = f["card"]
        card_type = card.get("Type")
        if card_type == "Leader":
            categories["Leader"].append(f)
        elif card_type == "Base":
            categories["Base"].append(f)
        else:
            cat = classify_aspect(card)
            if cat:
                categories[cat].append(f)
            categories["Wildcard"].append(f)
    return categories


def sheet_card_line(entry: dict) -> str:
    """Ligne 'Count Name' de la sheet."""
    return f"{entry['count']} {card_display_name(entry['card'])}"


def generate_draftmancer_file(cube_name: str, found: list[dict]) -> tuple[str, dict[str, list[dict]]]:
    """Génère le texte complet du fichier Draftmancer, et renvoie aussi les catégories (pour le résumé)."""
    categories = categorize_cards(found)

    settings = {
        "name": cube_name,
        "duplicateProtection": True,
    }

    parts = ["[Settings]", json.dumps(settings, ensure_ascii=False, indent=2)]

    custom_cards_json = [card_to_custom_card(f["card"]) for f in found]
    parts.append("[CustomCards]")
    parts.append(json.dumps(custom_cards_json, ensure_ascii=False, indent=2))

    for slot_name, count_per_pack in SLOT_ORDER:
        parts.append(f"[{slot_name}({count_per_pack})]")
        parts.extend(sheet_card_line(f) for f in categories[slot_name])

    return "\n".join(parts), categories


def main():
    parser = argparse.ArgumentParser(
        description="Lit un export de cube SWU (JSON) et récupère les cartes via l'API SWUDB."
    )
    parser.add_argument("cube_file", type=Path, help="Chemin vers le fichier JSON du cube")
    parser.add_argument("--cache-dir", type=Path, default=Path("cache"),
                         help="Dossier de cache pour les données de l'API (défaut : ./cache)")
    parser.add_argument("--only-set", type=str, default=None,
                         help="Ne traiter qu'un seul set (utile pour tester, ex: --only-set ASH)")
    parser.add_argument("--refresh-cache", action="store_true",
                         help="Force un nouveau téléchargement même si le cache existe")
    parser.add_argument("--output", type=Path, default=Path("draftmancer_cube.txt"),
                         help="Fichier de sortie Draftmancer (défaut : draftmancer_cube.txt)")
    args = parser.parse_args()

    cube = load_cube(args.cube_file)
    entries = extract_entries(cube)
    print_summary(entries)

    sets_needed = sorted({e["set"] for e in entries})
    if args.only_set:
        only = args.only_set.upper()
        if only not in sets_needed:
            sys.exit(f"Le set '{only}' n'est pas présent dans le cube.")
        sets_needed = [only]
        entries = [e for e in entries if e["set"] == only]
        print(f"\n(Mode test : on ne traite que le set {only})")

    print(f"\n--- Récupération de {len(sets_needed)} set(s) auprès de l'API ---")
    cards_by_set = {
        set_code: get_set_cards(set_code, args.cache_dir, refresh=args.refresh_cache)
        for set_code in sets_needed
    }
    for set_code, cards in cards_by_set.items():
        n_normal = sum(1 for c in cards if c.get("VariantType") == "Normal")
        print(f"  {set_code} : {len(cards)} carte(s) reçue(s) (dont {n_normal} variante 'Normal')")

    index, index_normalized = build_card_index(cards_by_set)
    found, missing = match_entries(entries, index, index_normalized)

    print(f"\n--- Correspondance cube <-> API ---")
    print(f"  {len(found)}/{len(entries)} carte(s) du cube retrouvée(s) dans l'API")
    if missing:
        print(f"  {len(missing)} carte(s) INTROUVABLE(S) :")
        for e in missing[:20]:
            print(f"    - {e['id']} (section: {e['section']})")
        if len(missing) > 20:
            print(f"    ... et {len(missing) - 20} de plus")
        diagnose_missing(missing, cards_by_set)

    if found:
        sample = found[0]["card"]
        print("\n--- Exemple de carte récupérée (pour vérification) ---")
        for key in ["Name", "Subtitle", "Type", "Aspects", "Rarity", "FrontArt", "BackArt"]:
            print(f"  {key:10s}: {sample.get(key)}")

        print("\n--- Conversion en CustomCards Draftmancer (aperçu) ---")
        custom_cards = [card_to_custom_card(f["card"]) for f in found]
        for c in custom_cards[:3]:
            print(" ", json.dumps(c, ensure_ascii=False))
        report_duplicate_names(found)

    if not missing:
        cube_name = cube.get("metadata", {}).get("name", "Cube SWU")
        file_text, categories = generate_draftmancer_file(cube_name, found)
        args.output.write_text(file_text, encoding="utf-8")

        print(f"\n--- Fichier Draftmancer généré : {args.output} ---")
        for slot_name, count_per_pack in SLOT_ORDER:
            n = len(categories[slot_name])
            print(f"  {slot_name:10s} : {n} carte(s) disponible(s) dans le cube "
                  f"(le booster en pioche {count_per_pack})")
    else:
        print("\n(Fichier Draftmancer non généré : il reste des cartes introuvables à corriger d'abord.)")


if __name__ == "__main__":
    main()
