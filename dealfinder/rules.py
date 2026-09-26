"""Rule reader: listing text in, facts out. Rules only (spec #1, D4)."""
import re
from dataclasses import dataclass

from .config import MIN_DISK_TB, MIN_MACHINE_BAYS, MIN_MACHINE_GEN

# model families, written once and reused by the form-factor and class rules
_ENTERPRISE_FAMILY = (r"\bexos\b(?!-)|\bultrastar\b|\bhc5\d\d\b|\bmg\d\d|\bwuh72|\bst\d{4,5}(nm|ne|nt)|\bwd\s?gold\b"
                      r"|\bironwolf\s?pro\b|\bred\s?pro\b")
_NAS_FAMILY = r"\bironwolf\b|\bred\s?plus\b|\bwd\s?red\b|\bst\d{4,5}vn"

_TB = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d)?)\s?tb\b", re.I)
_GB = re.compile(r"(?<![\d.,])(\d{2,4})\s?gb\b(?!\s?/\s?s)", re.I)  # "12Gb/s" is a link speed, not a size
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
    capacity_tb: float | None
    form_factor: str | None
    disk_class: str | None
    working: bool
    genuine: bool

    @property
    def qualifies(self):
        return (self.capacity_tb is not None and self.capacity_tb >= MIN_DISK_TB and self.form_factor == "3.5"
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
    sizes = {float(m.replace(",", ".")) for m in _TB.findall(title)} | {int(m) / 1000 for m in _GB.findall(title)}
    if not sizes and not _DISK_WORD.search(title):
        return None  # a family word alone is not a disk ("Exos" riflescopes and backpacks)
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
    too_small = bool(sizes) and max(sizes) < MIN_DISK_TB
    # a known disqualifying fact decides it: rejected, not "could not read"
    ruled_out = (too_small or form_factor in ("2.5", "external") or disk_class in ("external", "desktop")
                 or bool(_NON_GENUINE.search(title)) or condition == "for_parts" or bool(_FAULTY.search(title)))
    if missing and not ruled_out:
        return Unreadable(missing)
    capacity = (max(sizes) if too_small or len(sizes) != 1 else next(iter(sizes))) if sizes else None
    return DiskFacts(
        capacity_tb=capacity if capacity is None or not float(capacity).is_integer() else int(capacity),
        form_factor=form_factor,
        disk_class=disk_class,
        working=condition != "for_parts" and not _FAULTY.search(title),
        genuine=not _NON_GENUINE.search(title),
    )


# ---- Machines -------------------------------------------------------------------------------------------------

_DELL_MODEL = re.compile(r"\b([rt])(\d)(\d)(\d)(\d)?\s?(xd2|xd|xs|xa|xr)?\b", re.I)
_DELL_CONTEXT = re.compile(r"\bdell\b|poweredge|\bidrac", re.I)
_HP_MODEL = re.compile(r"\b(dl|ml)\s?(\d{3})[a-z]?\s?(?:gen\s?|g)(\d{1,2})\b", re.I)
_HP_GEN = {8: 12, 9: 13, 10: 14, 11: 16}  # HPE ProLiant Gen -> Dell generation number
_SUPERMICRO = re.compile(r"\bsupermicro\b", re.I)
_SERVER_WORD = re.compile(r"\bserver\b|superserver|\b[1-4]u\b|chassis|\bcse-\d|\bsys-\d", re.I)
# a part or accessory sold *for* a server, not the server
_FOR_SERVER = re.compile(
    r"\b(til|for|passer\s+til|passer|kompatibel\s+med|compatible\s+with|fits|suitable\s+for)\s+(\S+\s+){0,3}?"
    r"(dell|hpe?|poweredge|proliant|supermicro|[rt]\d{3}|dl\d{3}|ml\d{3}|gen\s?\d{1,2})\b", re.I)
_PART_NOUN = re.compile(
    r"fan\s?cage|\bvifte|heatsink|kjøler|\bbezel|batteri|battery|\briser\b|blindblende|blank\s+cover|drive\s?cage"
    r"|backplane|hovedkort|motherboard|mainboard|\bkabel\b|\bcable\b|rack\s?rails|rail\s?kit|\bjbod\b|diskhylle"
    r"|disk\s?shelf|\bvrtx\b|scania|\btekno\b|italeri|byggesett|samlermodell|smartmemory|\brdimm\b", re.I)
_STARTS_AS_PART = re.compile(r"^\W*(?:\S+\s+){0,2}?\d{1,4}\s?(?:gb|w)\b", re.I)  # "HPE 32GB ...", "500 W Power ..."

# CPU -> generation, for vendors without a generation in the model name
_CPU_GEN = ((re.compile(r"e5-?\s?2\d{3}[a-z]?\s?v[34]", re.I), 13), (re.compile(r"e5-?\s?2\d{3}[a-z]?\s?v2", re.I), 12),
            (re.compile(r"\b(?:bronze|silver|gold|platinum)\s?[3-8][12]\d\d", re.I), 14),
            (re.compile(r"\b(?:bronze|silver|gold|platinum)\s?[3-8]3\d\d", re.I), 15),
            (re.compile(r"\bepyc\s?7\d\d1", re.I), 14), (re.compile(r"\bepyc\s?7\d\d[23]", re.I), 15),
            (re.compile(r"\bx10[a-z]", re.I), 13), (re.compile(r"\bx11[a-z]", re.I), 14), (re.compile(r"\bx12[a-z]", re.I), 15),
            (re.compile(r"\b[ex]5[56]\d\d\b", re.I), 11))

# front 3.5" bays when the text says LFF but gives no count; only models with a single LFF front layout
_LFF_BAYS = {"r720": 8, "r720xd": 12, "r730": 8, "r730xd": 12, "r740": 8, "r740xd": 12, "r740xd2": 26,
             "r630": 4, "r640": 4}

_NUM_X = r"(?<![\w.,])(\d{1,2})\s?(?:x|×|\*|stk\.?)?\s?"  # "Gen9 LFF" is not 9 bays
_NOT_CONTENT = r"(?!\s*[\"”']?\s*(?:lff\s+)?(?:dell\s+)?(?:caddies|caddy|diskrammer|rammer|disker|disks|drives|hdd|harddisker|tb))"
_BAYS_35 = re.compile(
    _NUM_X + r"-?\s?(?:bay\s+)?(?:lff|3[.,]5\s?(?:\"|”|''|tommer|inch|in\b)?)" + _NOT_CONTENT, re.I)
_BAYS_25 = re.compile(_NUM_X + r"-?\s?(?:bay\s+)?(?:sff|2[.,]5\s?(?:\"|”|''|tommer|inch)?)", re.I)
_LFF_WORD = re.compile(r"\blff\b|3[.,]5\s?(\"|”|''|tommer|inch)", re.I)
# 2.5" words that describe the chassis, not a 2.5" SSD mentioned in passing
_SFF_CHASSIS = re.compile(r"\bsff\b|2[.,]5\s?[\"”']?\s?(?:sas\s+)?(?:backplane|bays?|diskplasser|diskslot\w*|front)", re.I)

_RAM_TOTAL = re.compile(r"(\d{2,4})\s?gb\b\s*(?:ddr\d|ram|ecc|minne|memory|rdimm|micron|total)", re.I)
_RAM_PRODUCT = re.compile(r"(\d{1,2})\s?[x×*]\s?(\d{1,3})\s?gb\b(?=[^\n]{0,25}(?:ddr|ram|dimm|ecc|minne|memory|brikker|pc[34]))", re.I)
_PSU_COUNT = re.compile(r"(\d)\s?[x×*]\s?(?:\S+\s+){0,2}?\d{3,4}\s?w\b|(\d)\s?[x×*]?\s?psu\b", re.I)
_PSU_TWO = re.compile(r"dual\s+psu|redundant\w*\s+(psu|power|strøm)|doble\s+strøm|2\s+strømforsyninger", re.I)
_PSU_ONE = re.compile(r"single\s+psu|\b1\s+psu\b", re.I)
_CADDY_NONE = re.compile(
    r"\b(no|ingen|uten|without)\s+(disk\s?)?(caddies|caddy|trays?|skuffer|diskrammer|rammer)\b"
    r"|\b(caddies|disk\s?trays?|trays|skuffer|diskrammer)\s+(følger\s+ikke|medfølger\s+ikke|not\s+included|mangler)", re.I)
_CADDY_COUNT = re.compile(
    r"(\d{1,2})\s?[x×*]\s?(?:3[.,]5\s?[\"”']?\s*(?:lff\s+)?|lff\s+)(?:dell\s+)?(?:caddies|caddy|diskrammer|rammer|trays)", re.I)
_RAID_ONLY = re.compile(r"\bh710p?\b|\bh700\b|\bh800\b|\bp410i?\b", re.I)
_HBA_CAPABLE = re.compile(r"hba\s?330|\bh[37][34]0p?\b|\bh750\b|\bh310\b|\bh200\b|\bp[48]40\w*\b|\bit[- ]mode\b|\bhba\b"
                          r"|\blsi\s?9[23]\d\d", re.I)
_RAILS_NONE = re.compile(r"(rails?|skinner|rackskinner|skinnepakke)\s+(mangler|følger\s+ikke|medfølger\s+ikke|not\s+included|missing)"
                         r"|\b(no|ingen|uten|without)\s+(rack\s?)?(rails?|skinner|rackskinner)", re.I)
_RAILS = re.compile(r"\brails?\b|\bskinner\b|rackskinner|skinnepakke|rail\s?kit", re.I)
_MACHINE_FAULTY = re.compile(r"\bdefekt|fungerer\s+ikke|virker\s+ikke|starter\s+ikke|not\s+working|for\s+parts|faulty|\bdoa\b", re.I)


@dataclass
class MachineFacts:
    vendor: str
    model: str
    generation: int | None
    amd: bool
    bays_35: int | None
    ram_gb: int | None
    ecc: bool
    psu_count: int | None
    caddies_35: int | None
    controller: str | None   # "hba" (passthrough possible), "raid" (RAID only), None = unknown
    rails: bool | None
    working: bool

    @property
    def qualifies(self):
        if self.generation is None or self.bays_35 is None:
            return False
        old_amd = self.amd and self.generation <= 13  # owner's decoder: avoid AMD 13th Gen or older
        return (self.generation >= MIN_MACHINE_GEN and not old_amd and self.bays_35 >= MIN_MACHINE_BAYS
                and self.ecc and self.working)


def _model(title, text):
    """(vendor, model, generation or None, amd) for a server named in the title, else None."""
    m = _HP_MODEL.search(title)
    if m:
        return "hpe", f"{m[1].upper()}{m[2]} Gen{m[3]}", _HP_GEN.get(int(m[3])), False
    m = _DELL_MODEL.search(title)
    if m and _DELL_CONTEXT.search(text):
        kind, d1, d2, d3, d4, suffix = m.groups()
        model = f"{kind}{d1}{d2}{d3}{d4 or ''}{suffix or ''}".upper().replace("XD", "xd")
        amd = (d3 + d4) in ("15", "25") if d4 else d3 == "5"
        return "dell", model, 10 + int(d2), amd
    if _SUPERMICRO.search(title) and _SERVER_WORD.search(title):
        return "supermicro", "Supermicro", None, bool(re.search(r"\bepyc\b", text, re.I))
    return None


def _cpu_generation(text):
    return next((gen for rx, gen in _CPU_GEN if rx.search(text)), None)


def _bays_35(model, text):
    m = _BAYS_35.search(text)
    if m:
        return int(m[1])
    if any(int(m[1]) >= 8 for m in _BAYS_25.finditer(text)):
        return 0  # an 8+ bay 2.5" front: no room for the 3.5" Disks
    if _LFF_WORD.search(text):
        return _LFF_BAYS.get(model.lower())
    if _SFF_CHASSIS.search(text):
        return 0
    return None


def _ram_gb(text):
    totals = [int(m[1]) for m in _RAM_TOTAL.finditer(text)]
    totals += [int(m[1]) * int(m[2]) for m in _RAM_PRODUCT.finditer(text)]
    totals = [t for t in totals if 8 <= t <= 3072]
    return max(totals) if totals else None


def _psu_count(text):
    m = _PSU_COUNT.search(text)
    if m:
        return int(m[1] or m[2])
    if _PSU_TWO.search(text):
        return 2
    return 1 if _PSU_ONE.search(text) else None


def _caddies_35(text):
    if _CADDY_NONE.search(text):
        return 0
    m = _CADDY_COUNT.search(text)
    return int(m[1]) if m else None


def _controller(text):
    if _RAID_ONLY.search(text):
        return "raid"
    return "hba" if _HBA_CAPABLE.search(text) else None


def _rails(text):
    if _RAILS_NONE.search(text):
        return False
    return True if _RAILS.search(text) else None


def read_machine(title, description, condition):
    """Facts for a Machine Listing, Unreadable when a required fact is missing, None when it is not a server."""
    text = f"{title}\n{description or ''}"
    if _FOR_SERVER.search(title) or _PART_NOUN.search(title) or _STARTS_AS_PART.search(title):
        return None
    found = _model(title, text)
    if found is None:
        return None
    vendor, model, generation, amd = found
    generation = generation or _cpu_generation(text)
    bays = _bays_35(model, text)
    missing = [name for name, value in (("generation", generation), ("bays_35", bays)) if value is None]
    if condition not in CONDITIONS:
        missing.append("condition")
    # cannot qualify, whatever else is missing: rejected, not "could not read"
    ruled_out = ((generation is not None and generation < MIN_MACHINE_GEN)
                 or (bays is not None and bays < MIN_MACHINE_BAYS))
    if missing and not ruled_out:
        return Unreadable(missing)
    return MachineFacts(
        vendor=vendor, model=model, generation=generation, amd=amd, bays_35=bays, ram_gb=_ram_gb(text),
        ecc=True,  # PowerEdge, ProLiant and Supermicro server boards take ECC RDIMMs only
        psu_count=_psu_count(text), caddies_35=_caddies_35(text), controller=_controller(text),
        rails=_rails(text), working=condition != "for_parts" and not _MACHINE_FAULTY.search(title))
