"""Rule reader: listing text in, facts out. Rules only (spec #1, D4)."""
import re
from dataclasses import dataclass

from .config import MIN_DISK_TB, MIN_MACHINE_BAYS, MIN_MACHINE_GEN, PLATFORMS

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
_HP_MODEL = re.compile(r"\b(dl|ml)\s?(\d{2,3})[a-z]?\s?(?:gen\s?|g)(\d{1,2})\b", re.I)
_HP_GEN = {8: 12, 9: 13, 10: 14, 11: 16}  # HPE ProLiant Gen -> Dell generation number
# single-socket models on a desktop socket (Xeon E3/E-2xxx): no CPU Part fits them
_DESKTOP_SOCKET = re.compile(r"[RT][1-3]\d0|(?:DL20|ML10|ML30) Gen\d+")
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
_CPU_WORD, _RAM_WORD = r"(?:cpus?|prosessor\w*|processors?)", r"(?:ram|memory|minne|dimms?)"


def _absent(noun):
    """'no CPU', 'No-CPU No-RAM', 'without CPUs or RAM', 'CPU/RAM not included', 'barebones'; not 'no memory errors'."""
    # after each noun: not "RAM 128GB", "RAM included" or "memory errors"
    faults = r"\s+(?:errors?|issues?|faults?|problems?|feil)"
    guard = rf"\b(?!\s*:?\s*\d+\s?(?:[x×*]\s?\d+\s?)?gb\b|\s+(?:included|inkl\w*)|{faults})"
    word = f"(?:{_CPU_WORD}|{_RAM_WORD})"

    def obj(join):
        return rf"(?:{word}{guard}{join})?{noun}{guard}(?:{join}{word}{guard})?(?!{join}{word}{faults})"
    join = r"\s*(?:/|&|,|\+|and|or|og|und|oder)\s*"
    # after "no", a bare space joins too: "No CPU RAM" means neither; "2x E5 CPU RAM not included" keeps its CPUs
    join_or_space = rf"(?:{join}|\s+)"
    return re.compile(rf"\b(?:no|ingen|uten|ohne|without|w/o)[\s-]+{obj(join_or_space)}"
                      rf"|\b{obj(join)}\s+(?:not\s+included|mangler|følger\s+ikke|medfølger\s+ikke)|\bbarebones?\b"
                      rf"|(?<![\w.,/])0\s?(?:gb\s?)?{noun}{guard}(?!\s+slots?)", re.I)  # "0 RAM", "0GB RAM"


_NO_RAM, _NO_CPU = _absent(_RAM_WORD), _absent(_CPU_WORD)
_CPU_MODEL = re.compile(r"e5-?\s?2\d{3}|\bxeon\b|\b(?:bronze|silver|gold|platinum)\s?\d{4}|\bepyc\b", re.I)
_PSU_COUNT = re.compile(r"(?<![\w.,])(\d)\s?[x×*]\s?(?:\S+\s+){0,2}?\d{3,4}\s?w\b|(?<![\w.,])(\d)\s?[x×*]?\s?psu\b", re.I)
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
    ram_gb: int | None       # 0 = the Listing says no RAM, None = not stated
    cpu: bool | None         # False = the Listing says no CPU or barebones, None = not stated
    sockets: int
    cpu_model: str | None    # normalised like read_cpu ("E5-2680 v4"), None = not stated
    cpu_count: int | None    # CPUs installed: 0 = none, None = not stated
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

    @property
    def platform(self):
        """The CPU socket this Machine takes; None when unsupported (16th Gen and newer, a desktop socket) or unknown."""
        if _DESKTOP_SOCKET.fullmatch(self.model) and not self.amd:
            return None
        return PLATFORMS.get((self.amd, self.generation))


def _model(title, text):
    """(vendor, model, generation or None, amd) for a server named in the title, else None."""
    m = _HP_MODEL.search(title)
    if m:
        return "hpe", f"{m[1].upper()}{m[2]} Gen{m[3]}", _HP_GEN.get(int(m[3])), m[2] in ("325", "385")
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


def _ram_in(text):
    totals = [int(m[1]) for m in _RAM_TOTAL.finditer(text)]
    totals += [int(m[1]) * int(m[2]) for m in _RAM_PRODUCT.finditer(text)]
    totals = [t for t in totals if 8 <= t <= 3072]
    return max(totals) if totals else None


def _ram_gb(title, text):
    """GB installed, 0 when none, None when not stated. The title wins: some eBay descriptions end with other
    servers from the same shop ("...2xE5-2680 V4 64 GB RAM"), so a "No RAM" title must not read 64 GB.
    In the description a stated amount beats a "no RAM", which may belong to another server there."""
    if _NO_RAM.search(title):
        return 0
    found = _ram_in(title)
    found = found if found is not None else _ram_in(text)
    return 0 if found is None and _NO_RAM.search(text) else found


def _cpu(title, text):
    """True when CPUs are installed, False when the Listing says none, None when not stated. Title wins; in the
    description a named CPU beats a "no CPU"."""
    if _NO_CPU.search(title):
        return False
    if _CPU_MODEL.search(text):  # text starts with the title
        return True
    return False if _NO_CPU.search(text) else None


# "2x", "2*", "2 stk.", "Dual", "two" right before the model, with "Intel® Xeon® Processor" or "10-core" between;
# or "x2" right after it
_COUNT_BEFORE = re.compile(r"(?:(?<![\w.,])(\d)\s?(?:[x×*]|stk\.?)|\b(dual|two))\s*"
                           r"(?:(?:intel|xeon|processor|®|™|\d+-?cores?)\s*)*$", re.I)
_COUNT_AFTER = re.compile(r"\s*[x×*]\s?(\d)\b")


def _cpu_installed(title, text):
    """(model, count) of the installed CPUs, title first like _cpu. A model named without a count is one CPU;
    (None, 0) when the Listing says none, (None, None) when not stated."""
    if _NO_CPU.search(title):
        return None, 0
    for where in (title, text):  # text starts with the title
        found = _cpus(where)
        if found:
            m, model, _ = found[0]
            n = _COUNT_BEFORE.search(where[max(0, m.start() - 40):m.start()])
            after = _COUNT_AFTER.match(where, m.end())
            return model, int(after[1]) if after else int(n[1]) if n and n[1] else 2 if n else 1  # else one
    return (None, 0) if _cpu(title, text) is False else (None, None)


def _sockets(vendor, model, text):
    """Dell R1x0-R3x0/T1x0-T3x0 = 1; Dell AMD names the count in its third digit (R6415, R7515 = 1, R7425 = 2);
    HPE DL20/DL325/ML10/ML30/ML110 = 1; a Supermicro board X..S.. = 1 (X10SRi), X..D.. = 2; anything else 2."""
    if vendor == "dell":
        digits = re.sub(r"\D", "", model)
        return 1 if (digits[2] == "1" if len(digits) == 4 else digits[0] in "123") else 2
    if vendor == "hpe":
        return 1 if model.split()[0] in ("DL20", "DL325", "ML10", "ML30", "ML110") else 2
    board = re.search(r"\b[xh]1\d([ds])", text, re.I)
    return 1 if board and board[1].lower() == "s" else 2


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
    cpu_model, cpu_count = _cpu_installed(title, text)
    return MachineFacts(
        vendor=vendor, model=model, generation=generation, amd=amd, bays_35=bays, ram_gb=_ram_gb(title, text), cpu=_cpu(title, text),
        sockets=_sockets(vendor, model, text), cpu_model=cpu_model, cpu_count=cpu_count,
        ecc=True,  # PowerEdge, ProLiant and Supermicro server boards take ECC RDIMMs only
        psu_count=_psu_count(text), caddies_35=_caddies_35(text), controller=_controller(text),
        rails=_rails(text), working=condition != "for_parts" and not _MACHINE_FAULTY.search(title))


# ---- CPUs -----------------------------------------------------------------------------------------------------

# "E5-2680 v4" (E5-16xx is a workstation part, x00 is a family: "E5-2600 v3"), "Gold 6130", "EPYC 7302P";
# "2xE5-2680" is a count glued to the model, so E5 may follow an "x"
_CPU_NAME = re.compile(
    r"(?<![a-wyz])e5[-\s]?(?P<e5>[24][46](?!00)\d\d)(?P<e5s>[lwa]?)(?:\s?v(?P<v>[1-4]))?\b"
    r"|\b(?P<tier>bronze|silver|gold|platinum)\s?(?P<sp>[3-9][1-5]\d\d)(?P<sps>[a-z]{0,2})\b"
    r"|\bepyc\s?(?P<epyc>7(?!00)[\dfhb]{2}[1-3])(?P<epycs>p?)\b", re.I)  # 7302, 7F52, 7H12, 74F3
_CPU_TEXT = re.compile(rf"\bxeon\b|\bepyc\b|\b{_CPU_WORD}", re.I)
# a desktop or laptop CPU, a whole computer, a CPU + board combo, or a cooler: not a CPU Listing.
# E3/E-2xxx and E5-16xx are desktop-socket or single-socket workstation CPUs, not Machine Parts
_NOT_A_CPU = re.compile(
    r"\bi[3579]\b|\bryzen\b|threadripper|\bceleron\b|\bpentium\b|\be3[-\s]?1\d{3}|\be5[-\s]?1\d{3}|\be-2\d{3}"
    r"|\bpc\b|(?<!/)workstation|arbeidsstasjon|mac\s?pro|precision|thinkstation|laptop|notebook|rack\s?server"
    r"|\b[1-4]u\b|\b\d{1,4}\s?gb\b|\s\+\s|combo|hovedkort|motherboard|mainboard"
    r"|cooler|heatsink|kjøle|\bfans?\b|vifte", re.I)
# servers from vendors outside the decoder, and a bare "server" in a title with no CPU word; "til server/...",
# "for server" and "Server CPU" are CPUs
_SERVER_TEXT = re.compile(r"thinksystem|primergy|\bucs\b|\bproliant\b", re.I)
_BARE_SERVER = re.compile(r"(?<!\btil\s)(?<!\bfor\s)(?<!\bin\s)\bserver\b(?!/)", re.I)
_KIT = re.compile(r"\bkit\b", re.I)  # "ProLiant DL360 Gen10 - Xeon Gold 6130 CPU 1 Kit" is a CPU, not a server
# not "48x PCIe", "3x UPI", "2 x QPI" links, cores and clock ("8 x 2.10 GHz", "8x cores"),
# nor stock on hand: "(4 pcs available)"
_UNITS = re.compile(r"(?<![\w.,/-])([1-9]\d?)\s?(?:[x×*]|pcs|pieces|stk|units|kit)"
                    r"(?!\s*(?:pci|upi|qpi|available|cores?|kjerner|threads|\d+(?:[.,]\d+)?\s?ghz))"
                     r"|\bx\s?([2-8])\b(?![.,]\d|\s?ghz)"
                    r"|\blot\s+of\s+(\d{1,2})\b", re.I)
_PAIR = re.compile(r"\bpairs?\b|\b\w*par\b", re.I)  # "matchet prosessorpar"
_SUPPORTED_SOCKETS = set(PLATFORMS.values())


@dataclass
class CpuFacts:
    vendor: str
    model: str | None
    platform: str | None   # socket: LGA2011-3, LGA3647, LGA4189, SP3, or an older/newer one that no Machine takes
    count: int             # CPUs this one Listing sells
    working: bool

    @property
    def qualifies(self):
        return self.working and self.model is not None and self.platform in _SUPPORTED_SOCKETS


def _cpus(text):
    """(match, model, socket) for every CPU model named in `text`."""
    found = []
    for m in _CPU_NAME.finditer(text):
        if m["e5"]:
            model = f"E5-{m['e5']}{m['e5s'].upper()}" + (f" v{m['v']}" if m["v"] else "")
            socket = "LGA1356" if m["e5"][1] == "4" else "LGA2011-3" if m["v"] in ("3", "4") else "LGA2011"
        elif m["sp"]:
            model = f"{m['tier'].title()} {m['sp']}{m['sps'].upper()}"
            socket = {"1": "LGA3647", "2": "LGA3647", "3": "LGA4189"}.get(m["sp"][1], "LGA4677")
        else:
            model, socket = f"EPYC {m['epyc'].upper()}{m['epycs'].upper()}", "SP3"
        found.append((m, model, socket))
    return found


def read_cpu(title, condition):
    """Facts for a CPU Listing, Unreadable when the model or socket is missing, None when it is not a CPU
    (a whole server, a desktop or laptop CPU, a cooler)."""
    cpus = _cpus(title)
    if (not cpus and not _CPU_TEXT.search(title)) or _NOT_A_CPU.search(title):
        return None
    server = (_model(title, title) or _SERVER_TEXT.search(title)
              or (_BARE_SERVER.search(title) and not re.search(rf"\b{_CPU_WORD}", title, re.I)))
    if server and not _KIT.search(title) and not _FOR_SERVER.search(title):
        return None  # a whole server in a CPU search
    models, sockets = {c[1] for c in cpus}, {c[2] for c in cpus}
    model = models.pop() if len(models) == 1 else None  # several models: a multi-choice Listing
    platform = sockets.pop() if len(sockets) == 1 else None
    working = condition != "for_parts" and not _FAULTY.search(title)
    missing = [name for name, value in (("model", model), ("platform", platform)) if value is None]
    # a known disqualifying fact decides it: rejected, not "could not read"
    ruled_out = not working or platform not in (None, *_SUPPORTED_SOCKETS)
    if missing and not ruled_out:
        return Unreadable(missing)
    n = _UNITS.search(title)
    count = max(1, int(next(g for g in n.groups() if g))) if n else 2 if _PAIR.search(title) else 1  # not "lot of 0"
    return CpuFacts(vendor="amd" if re.search(r"\bepyc\b|\bamd\b", title, re.I) else "intel", model=model,
                    platform=platform, count=count, working=working)
