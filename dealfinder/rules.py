"""Rule reader: listing text in, facts out. Rules only (spec #1, D4)."""
import re
from dataclasses import dataclass

from .config import MIN_DISK_TB

# model families, written once and reused by the form-factor and class rules
_ENTERPRISE_FAMILY = r"exos|ultrastar|\bhc5\d\d\b|\bmg\d\d|wuh72|\bst\d{4,5}(nm|ne|nt)|wd\s?gold|ironwolf\s?pro|red\s?pro"
_NAS_FAMILY = r"ironwolf|red\s?plus|wd\s?red|\bst\d{4,5}vn"

_TB = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d)?)\s?tb\b", re.I)
_DISK_WORD = re.compile(r"\bhdd\b|hard\s?(disk|drive)|harddisk|festplatte|\bdisk\b", re.I)
_QUANTITY = re.compile(r"\blot\b|\b[2-9]\d?\s?x\s|\b\d+\s?(pcs|pieces|stk)\b", re.I)
_FF_35 = re.compile(r"(?<![\d.,])3[.,]5(?!\d)|\blff\b|8[.,]9\s?cm", re.I)
_FF_25 = re.compile(r"(?<![\d.,])2[.,]5(?!\d)(?!\s?(gbe?|gbit|g\b|l\b))|\bsff\b", re.I)
_FAMILY_35 = re.compile(f"{_ENTERPRISE_FAMILY}|{_NAS_FAMILY}", re.I)
_EXTERNAL = re.compile(r"\bexternal\b|\bextern\w*|\busb\b|\belements\b|my\s?book|expansion\s?desktop|one\s?touch", re.I)
_ENTERPRISE = re.compile(rf"{_ENTERPRISE_FAMILY}|\benterprise\b|data\s?cent(er|re)", re.I)
_NAS = re.compile(rf"{_NAS_FAMILY}|\bnas\b", re.I)
_DESKTOP = re.compile(r"barracuda|\bdesktop\b|wd\s?blue|\bpurple\b|skyhawk", re.I)
# relabel phrasing: nobody sells a genuine drive "for" its own maker ("for Dell/Lenovo" OEM drives stay genuine)
_NON_GENUINE = re.compile(
    r"\bcompatible\b|suitable\s+for|\bfits?\s+for\b|\bfor\s+(seagate|toshiba|wd|western\s+digital|hgst)\b", re.I)
_ACCESSORY = re.compile(
    r"\bpcb\b|\b(controller|logic)\s+board\b|\b(caddy|caddies|tray|sled|carrier|bracket|adapter|cable|enclosure)\s+(for|fits?)\b"
    r"|\bempty\s+box\b|\bbox\s+only\b", re.I)
_FAULTY = re.compile(r"faulty|defekt|defect|\bparts\b|spares|broken|\bdead\b|not\s+working|\bdoa\b", re.I)

CONDITIONS = ("new", "refurbished", "used", "for_parts")


@dataclass
class Unreadable:
    missing: list


@dataclass
class DiskFacts:
    capacity_tb: float
    form_factor: str
    disk_class: str
    working: bool
    genuine: bool

    @property
    def qualifies(self):
        return (self.capacity_tb >= MIN_DISK_TB and self.form_factor == "3.5"
                and self.disk_class in ("enterprise", "nas") and self.working and self.genuine)


def _disk_class(title):
    for name, rx in (("external", _EXTERNAL), ("enterprise", _ENTERPRISE), ("nas", _NAS), ("desktop", _DESKTOP)):
        if rx.search(title):
            return name
    return None


def _form_factor(title):
    has35, has25 = bool(_FF_35.search(title)), bool(_FF_25.search(title))
    if has35 != has25:
        return "3.5" if has35 else "2.5"
    if not has35 and _FAMILY_35.search(title):
        return "3.5"
    return None


def read_disk(title, condition):
    """Facts for a Disk Listing, Unreadable when a required fact is missing, None when it is not a disk at all.

    `condition` is the Source's normalised condition: one of CONDITIONS, or None when the Source did not say.
    """
    sizes = {float(m.replace(",", ".")) for m in _TB.findall(title)}
    if not sizes and not _DISK_WORD.search(title) and not _FAMILY_35.search(title):
        return None
    if _ACCESSORY.search(title):  # a part or accessory for a disk, not a disk
        return None
    missing = []
    if len(sizes) != 1:
        missing.append("capacity")
    disk_class = _disk_class(title)
    if disk_class is None:
        missing.append("disk_class")
    # an external USB enclosure is rejected on class alone; its bay size is irrelevant
    form_factor = "external" if disk_class == "external" else _form_factor(title)
    if form_factor is None:
        missing.append("form_factor")
    if condition not in CONDITIONS:
        missing.append("condition")
    if _QUANTITY.search(title):
        missing.append("quantity")
    if missing:
        return Unreadable(missing)
    capacity = sizes.pop()
    return DiskFacts(
        capacity_tb=int(capacity) if capacity.is_integer() else capacity,
        form_factor=form_factor,
        disk_class=disk_class,
        working=condition != "for_parts" and not _FAULTY.search(title),
        genuine=not _NON_GENUINE.search(title),
    )
