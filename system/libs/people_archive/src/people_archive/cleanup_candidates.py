"""Broad candidate blocking; matching never implies an identity decision."""
import itertools
import re
import unicodedata
from collections import defaultdict
from typing import Any


def normalized(value: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in unicodedata.normalize("NFKC",value).casefold()).split())


def candidate_pairs(people: list[dict[str,Any]], facts: dict[str,list[dict[str,Any]]]) -> set[tuple[str,str]]:
    blocks: dict[str,set[str]] = defaultdict(set)
    short_names: dict[str,set[str]] = defaultdict(set)
    first_names: dict[str,set[str]] = defaultdict(set)
    nicknames = {"bob":"robert","rob":"robert","bobby":"robert","bill":"william","will":"william","liz":"elizabeth","beth":"elizabeth","kate":"katherine","katie":"katherine","mike":"michael","matt":"matthew","jim":"james","jimmy":"james","joe":"joseph","meg":"meaghan","josh":"joshua","dan":"daniel","dave":"david","chris":"christopher","alex":"alexander"}
    for person in people:
        identity = person["id"]
        names = [person["name"], *person["aliases"]]
        handles = list(person["handles"])
        for fact in facts.get(identity,[]):
            if fact["field"] == "name":
                names.append(fact["value"])
            elif fact["field"] in ("email","phone","profile"):
                handles.append(fact["field"] + ":" + fact["value"])
        for name in names:
            clean = normalized(name)
            parts = clean.split()
            if parts:
                first = nicknames.get(parts[0],parts[0])
                first_names[first].add(identity)
                if len(parts) == 1 and not any(c.isdigit() for c in first):
                    short_names[first].add(identity)
            if len(parts) >= 2:
                blocks["nickname:"+parts[-1]+":"+first].add(identity)
                blocks["name:"+clean].add(identity)
                blocks["surname:"+parts[-1]+":"+parts[0][0]].add(identity)
                # Nearby spelling differences, including middle names and nicknames, enter review.
                blocks["fuzzy:"+parts[-1][:4]+":"+parts[0][:2]].add(identity)
        for handle in handles:
            if handle.startswith("phone:"):
                handle = "phone:" + re.sub(r"[^0-9]","",handle[6:])
            blocks["handle:"+handle.casefold()].add(identity)
            if handle.startswith("email:"):
                local = handle[6:].split("@")[0].split("+")[0]
                blocks["local:"+re.sub(r"[._-]","",local).casefold()].add(identity)
    pairs = {pair for members in blocks.values() for pair in itertools.combinations(sorted(members),2)}
    pairs.update(tuple(sorted((a,b))) for first,shorts in short_names.items() for a in shorts for b in first_names[first] if a != b)
    return pairs
