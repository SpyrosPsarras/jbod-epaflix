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
    r"fan\s?cage|\bvifte|\bbezel|batteri|battery|\briser\b|blindblende|blank\s+cover|drive\s?cage"
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
_STICK_SIZES = {4, 8, 16, 32, 64, 128}  # GB; a "256 GB" alone is a total of unknown sticks, "(2x 480GB)" SSDs
# "8x16GB DDR4", or right after a total: "128GB (8x16GB)"
_RAM_PRODUCT = re.compile(r"(\d{1,2})\s?[x×*]\s?(\d{1,3})\s?gb\b"
                          r"(?=\)|[^\n]{0,25}(?:ddr|ram|dimm|ecc|minne|memory|brikker|pc[34]))", re.I)
# a speed in MT/s: "DDR4-2400", "2400MHz", "2933MT/s", "2666V", "PC4-2400T", or a PC rating "PC4-19200";
# not a CPU model "E5-2666 v3" or a PSU "1600W"
_SPEED = re.compile(r"(?<![\w.,])(?<!e5-)(\d{4,5})(?=\s?(?:mhz|mt/?s)|[a-z]{0,2}\b)(?!\s?w\b)", re.I)
_PC_RATING = {8500: 1066, 10600: 1333, 12800: 1600, 14900: 1866, 17000: 2133, 19200: 2400, 21300: 2666, 23400: 2933,
              25600: 3200}
_SPEEDS = set(_PC_RATING.values())
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
# "heatsink", "heat sink", "kjøleribbe", "HS" ("2xHS"); "cooler" and "kjøler" also name desktop and laptop coolers
# bare "HS" before a PSU, wattage, bays or disks means hot-swap: "2x HS PSU", "2x HS 800W", "8x HS SAS"
_HS_BARE = r"(?<![a-wyz])hs\b(?!\s*(?:psu|power|\d{3,4}\s?w|bays?|sas|sata|lff|sff|caddies|drives?)\b)"
_HS_STRONG = re.compile(rf"heat\s?-?sinks?|kjøleribb\w*|{_HS_BARE}", re.I)
_HS_WORD = rf"(?:{_HS_STRONG.pattern}|coolers?\b|kjøler(?:e|en|ne)?\b)"
_NO_HS = _absent(_HS_WORD)
_HS_BETWEEN = r"(?:(?:cpu|high|performance|standard|std|low|profile|dell|hpe?)[\s-]+){0,3}"
# "2xHS", "2x Cooler", "2 stk kjøler"
_HS_COUNT = re.compile(rf"(?<![\w.,])(\d)\s?(?:[x×*]|stk\.?|pcs)?\s?{_HS_BETWEEN}{_HS_WORD}", re.I)
_HS_ONE = re.compile(rf"\b(?:with|w/|med|inkl\w*\.?|incl\w*\.?)\s*{_HS_BETWEEN}"
                     rf"(?:heat\s?-?sink\b|{_HS_BARE}|cooler\b|kjøler(?:en)?\b|kjøleribbe\b)", re.I)  # singular only
_HS_PART = re.compile(r"heat\s?-?sink|kjøler", re.I)  # a Machine part, unless it states a Machine fact
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
    ram_sticks: dict | None  # {"count", "gb", "speed" (MT/s or None)} of the installed sticks, None = not stated
    cpu: bool | None         # False = the Listing says no CPU or barebones, None = not stated
    sockets: int
    cpu_model: str | None    # normalised like read_cpu ("E5-2680 v4"), None = not stated
    cpu_count: int | None    # CPUs installed: 0 = none, None = not stated
    heatsinks: int | None    # CPU heatsinks included: 0 = none, None = not stated
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


def _dell_model(m):
    """'R730xd' from a _DELL_MODEL match."""
    return "".join(g or "" for g in m.groups()).upper().replace("XD", "xd")


def _model(title, text):
    """(vendor, model, generation or None, amd) for a server named in the title, else None."""
    m = _HP_MODEL.search(title)
    if m:
        return "hpe", f"{m[1].upper()}{m[2]} Gen{m[3]}", _HP_GEN.get(int(m[3])), m[2] in ("325", "385")
    m = _DELL_MODEL.search(title)
    if m and _DELL_CONTEXT.search(text):
        _, _, d2, d3, d4, _ = m.groups()
        amd = (d3 + d4) in ("15", "25") if d4 else d3 == "5"
        return "dell", _dell_model(m), 10 + int(d2), amd
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
    totals += [int(m[1]) * int(m[2]) for m in _RAM_PRODUCT.finditer(text) if int(m[2]) in _STICK_SIZES]
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


def _speed(text):
    """The one DDR speed in MT/s stated in `text`, None when none or several."""
    found = {_PC_RATING.get(n, n) for n in map(int, _SPEED.findall(text))} & _SPEEDS
    return found.pop() if len(found) == 1 else None


def _ram_sticks(title, text, ram_gb):
    """{count, gb, speed} of the installed sticks, title first; only sticks that add up to ram_gb count, so another
    server listed further down the description is not read. None when not stated."""
    found = [(int(m[1]), int(m[2]), _speed(where[max(0, m.start() - 40):m.end() + 40]))
             for where in (title, text) for m in _RAM_PRODUCT.finditer(where)  # text starts with the title
             if int(m[2]) in _STICK_SIZES and int(m[1]) * int(m[2]) == ram_gb]
    if not found:
        return None
    count, gb, _ = found[0]
    # the same sticks may be named twice, the speed only once: "128 GB RAM (2x 64GB)" ... "128GB DDR4-2400T (2x 64GB)"
    return {"count": count, "gb": gb, "speed": next((s for c, g, s in found if (c, g) == (count, gb) and s), None)}


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


def _heatsinks(title, text):
    """CPU heatsinks included, title first: the number stated ("2xHS", "2x Cooler"), 1 for a singular "with CPU
    Cooler" (taken literally, even on a dual-socket Machine), 0 when the title says "no heatsinks" or barebones,
    None when not stated. "with heatsinks" gives no number: None."""
    for where in (title, text):  # text starts with the title
        m = _HS_COUNT.search(where)
        if m:
            return int(m[1])
        if _NO_HS.search(title):  # the title only: a "barebone No CPU" in a description is often another server
            return 0
        if _HS_ONE.search(where):
            return 1
    return None


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
    hs_part = _HS_PART.search(title) and not (_HS_COUNT.search(title) or _NO_HS.search(title) or _HS_ONE.search(title))
    if (_FOR_SERVER.search(title) or _PART_NOUN.search(title) or hs_part or _STARTS_AS_PART.search(title)
            or read_heatsink(title, condition) is not None):
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
    ram_gb = _ram_gb(title, text)
    return MachineFacts(
        vendor=vendor, model=model, generation=generation, amd=amd, bays_35=bays, ram_gb=ram_gb,
        ram_sticks=_ram_sticks(title, text, ram_gb), cpu=_cpu(title, text),
        sockets=_sockets(vendor, model, text), cpu_model=cpu_model, cpu_count=cpu_count,
        heatsinks=_heatsinks(title, text),
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


# ---- RAM ------------------------------------------------------------------------------------------------------

_RAM_TEXT = re.compile(r"dimm|\bram\b|minne|memory|\bddr[2-5]|\bpc[2-5]l?-|registered|registrert", re.I)
# desktop, laptop, NAS or unbuffered memory, or a whole computer: not a server RAM Listing
_NOT_RAM = re.compile(
    r"udimm|unbuffered|so-?dimm|\blaptop|notebook|bærbar|\b(?:200|204|260)-?pin|vengeance|hyperx|\bfury\b|g\.?skill"
    r"|ripjaws|trident|ballistix|dominator|\brgb\b|non[-\s]?ecc|\bi[3579]\b|ryzen|\b\d+\s?tb\b|\b[1-4]u\b|\bnas\b"
    r"|\bidrac|kjerner|\bcores?\b|motherboard|mainboard|hovedkort", re.I)
_WORKSTATION = re.compile(r"workstation|precision|\bz[468]\d0\b", re.I)
_RAM_GB = re.compile(r"(?<![\w.,])(\d{1,3})\s?gb\b(?!\s?/\s?s)", re.I)
# "4x 16GB", "4 x 16GB", or "8GB(X4)"
_STICKS_X = re.compile(r"(?<![\w.,])(\d{1,2})\s?[x×*]\s?(\d{1,3})\s?gb\b"
                       r"|(?<![\w.,])(\d{1,3})\s?gb\s?\(?[x×*]\s?(\d{1,2})\b", re.I)
# "8x Samsung", "8 stk", "4 pcs.", "× 4 st.", "kit of 8", "lot of 4"; not "1x 2Rx4" or "Dual Rank x4", nor stock on hand:
# "(4 pcs available)", "10 stk på lager", "har 12 stk"
_STICK_COUNT = re.compile(
    r"(?<![\w.,/-])(?<!har\s)(\d{1,2})\s?(?:[x×*](?!\s?\d)|stk\b|pcs\b|pieces\b|st\.)"
    r"(?!\s*(?:available|tilgjengelig|på\s+lager|ledig))"
    r"|(?<!\w)[x×]\s?(\d{1,2})\s?(?:st|stk|pcs)\b|\b(?:kit|lot|set|sett)\s+(?:of|med|på)\s+(\d{1,2})\b", re.I)
# the price is for one stick, whatever count the title names.
# ponytail: without these words the count is trusted, so a finn.no "10x Samsung 32GB" priced per stick reads as
# 10 sticks at a tenth of the real NOK per GB; a NOK-per-GB floor would catch it if such Listings top Best RAM
_PER_STICK = re.compile(r"\bpris\s+per\s+st\w*|\bpr\.?\s+st(?:k|ykk)\b"
                        r"|\bper\s+(?:stk|stykk|brikke|modul|stick|module|piece)\b|\beach\b", re.I)
_DDR = re.compile(r"\bddr\s?([2-5])(?!\d)|\bpc([2-5])l?-", re.I)
# labels: "PC4-2400T-R" / "PC4-17000R" registered, "PC3-14900L" / "PC4-2133P-LD0" load reduced
_LRDIMM = re.compile(r"lrdimm|load[\s-]?reduced|\bpc[34]l?-\d{4,5}[a-z]{0,2}-?l", re.I)
_RDIMM = re.compile(r"(?<!l)rdimm|registered|registrert|\breg\b|\bpc[34]l?-\d{4,5}[a-z]{0,2}-?r", re.I)


@dataclass
class RamFacts:
    gb_per_stick: int | None
    sticks: int              # sticks this one Listing sells
    ddr: int | None          # DDR generation
    type: str | None         # "RDIMM" or "LRDIMM"
    speed: int | None        # MT/s
    ecc: bool
    working: bool

    @property
    def qualifies(self):
        return self.working and self.ddr == 4 and self.ecc and self.type is not None and self.gb_per_stick is not None


def read_ram(title, condition):
    """Facts for a RAM Listing, Unreadable when the stick size, DDR generation or type is missing, None when it is
    not server RAM (desktop, laptop or unbuffered memory, a whole server or PC)."""
    if not _RAM_TEXT.search(title) or _NOT_RAM.search(title) or _CPU_TEXT.search(title) or _cpus(title):
        return None
    if _WORKSTATION.search(title) and not _FOR_SERVER.search(title):
        return None  # a whole workstation; "RAM for Dell Precision T7810" is RAM
    server = _model(title, title) or _SERVER_TEXT.search(title)
    if server and not (_FOR_SERVER.search(title) or _PART_NOUN.search(title) or _STARTS_AS_PART.search(title)):
        return None  # a whole server in a RAM search; "HPE 32GB ... DL380 Gen9" and "... for Dell R730" are RAM
    product = next(((int(m[1] or m[4]), int(m[2] or m[3])) for m in _STICKS_X.finditer(title)
                    if int(m[2] or m[3]) in _STICK_SIZES and 1 <= int(m[1] or m[4]) <= 32), None)  # not "32x2 GB"
    if product:
        sticks, gb = product
    else:
        n = _STICK_COUNT.search(title)
        sticks = max(1, int(next(g for g in n.groups() if g))) if n else 2 if _PAIR.search(title) else 1
        sizes = {int(s) for s in _RAM_GB.findall(title)}
        sizes = {s for s in sizes if s * sticks in sizes} or sizes  # "kit of 4 16GB 64GB": 16 per stick
        sizes &= _STICK_SIZES
        gb = sizes.pop() if len(sizes) == 1 else None
    sticks = 1 if _PER_STICK.search(title) else sticks
    gens = {int(a or b) for a, b in _DDR.findall(title)}
    ddr = gens.pop() if len(gens) == 1 else None
    ram_type = "LRDIMM" if _LRDIMM.search(title) else "RDIMM" if _RDIMM.search(title) else None
    working = condition != "for_parts" and not _FAULTY.search(title)
    missing = [name for name, value in (("gb_per_stick", gb), ("ddr", ddr), ("type", ram_type)) if value is None]
    # a known disqualifying fact decides it: rejected, not "could not read" (DDR3 is readable, only DDR4 qualifies)
    if missing and working and ddr in (None, 4):
        return Unreadable(missing)
    return RamFacts(gb_per_stick=gb, sticks=sticks, ddr=ddr, type=ram_type, speed=_speed(title),
                    ecc=ram_type is not None or bool(re.search(r"\becc\b", title, re.I)), working=working)


# ---- Heatsinks ------------------------------------------------------------------------------------------------

_HS_ANY = re.compile(_HS_WORD, re.I)
# desktop, laptop, GPU, SSD and board coolers, sockets no Machine takes (Gen10 Plus is LGA4189)
_NOT_HS = re.compile(
    r"noctua|cooler\s?master|\baio\b|tower\s?cooler|arctic|be\s?quiet|deepcool|thermalright|zalman|water|liquid"
    r"|væske|lga\s?(?:115\d|1200|1700|1851|775)|\bam[2-5]\b|socket\s?(?:462|775)|\bsp5\b|lga\s?4677|\bg34\b"
    r"|opteron|\b13[56]6\b|(?<!cpu\W)\bgpu\s?(?:heat\s?-?sinks?|coolers?)|quadro|\b[rg]tx\b|\bvga\b|graphics|nvidia"
    r"|geforce|radeon|tesla|\bi[3579]\b|ryzen|laptop|notebook|bærbar|samsung|\bnp-|\bssd|nvme|m\.2|raspberry"
    r"|\bg(?:en)?\s?10\s?(?:plus|\+)", re.I)
# heatsink brackets and clips; "Heatsink ... w/Bracket" sells the heatsink
_HS_MOUNT = re.compile(r"holder|bracket|\bclips?\b|clamp|mounting|\bbase\b", re.I)
_WITH_END = re.compile(r"(?:w/|with|med|incl\w*\.?|inkl\w*\.?|\+|&)\s*$", re.I)
_FAN = re.compile(r"(?<!server )\bfans?\b|vifte|blower", re.I)  # "Heatsink Server Fan" is a listing category
_RACK_SERVER = re.compile(r"\brack\s?server\b", re.I)
# a heatsink word with no Machine model is worth reading only with a server word; else an SSD or Raspberry Pi one
_SERVER_CONTEXT = re.compile(r"server|poweredge|proliant|\bdell\b|\bhpe?\b|xeon|lga\s?(?:2011|3647|4189)", re.I)
# "DL380 Gen9", and several models sharing one Gen: "DL380 DL388 G9", "DL380/388 Gen9"
_HP_MODELS = re.compile(r"\b(dl|ml)\s?(\d{2,3})[a-z]?\b"
                        r"(?=(?:[\s/,&+]+(?:(?:dl|ml)\s?)?\d{2,3}[a-z]?\b)*[\s/,&+-]*(?:gen\s?|g)(\d{1,2})\b)", re.I)
_SUPERMICRO_HS = re.compile(r"super\s?micro|\bsnk-p\d", re.I)


@dataclass
class HeatsinkFacts:
    fits: list      # Machine models, named like MachineFacts.model: "R730xd", "DL380 Gen9", "Supermicro"
    count: int      # heatsinks this one Listing sells
    working: bool

    @property
    def qualifies(self):
        return self.working and bool(self.fits)


def read_heatsink(title, condition):
    """Facts for a CPU heatsink Listing, Unreadable when it names no Machine model, None when it is not a server
    CPU heatsink (a fan, a whole server, a CPU, a desktop, laptop, GPU or SSD cooler)."""
    mount = _HS_MOUNT.search(title)
    if not _HS_ANY.search(title) or _NOT_HS.search(title) or mount and not _WITH_END.search(title[:mount.start()]):
        return None
    if _FAN.search(title) and not _KIT.search(title):
        return None  # "R740 Heatsink Fans 0N5T36" is a fan; "Heatsink 747608-001 & 2 Fans CPU Kit" is a heatsink
    cpus = [m for m, _, _ in _cpus(title) if not re.search(r"up\s+to\s*$", title[:m.start()], re.I)]
    bays = any(int(m[1]) >= MIN_MACHINE_BAYS for rx in (_BAYS_35, _BAYS_25) for m in rx.finditer(title))
    if (cpus or bays or _ram_in(title) or _NO_CPU.search(title) or _NO_RAM.search(title) or _NO_HS.search(title)
            or _RACK_SERVER.search(title)):
        return None  # a whole server, or a CPU with its heatsink; "Heatsink up to E5-2660V3" is a heatsink
    # ponytail: fits names the models only, no part numbers (0YY2R8 = R730) and no socket or 1U/2U height, so a
    # Supermicro heatsink fits every Supermicro. Read the socket (LGA2011, LGA3647) if Supermicro Builds get used
    fits = {_dell_model(m) for m in _DELL_MODEL.finditer(title)}
    fits |= {f"{m[1].upper()}{m[2]} Gen{m[3]}" for m in _HP_MODELS.finditer(title)}
    fits |= {"Supermicro"} if _SUPERMICRO_HS.search(title) else set()
    working = condition != "for_parts" and not _FAULTY.search(title)
    if not fits:
        return Unreadable(["fits"]) if working and _HS_STRONG.search(title) and _SERVER_CONTEXT.search(title) else None
    n = _UNITS.search(title)
    count = max(1, int(next(g for g in n.groups() if g))) if n else 2 if _PAIR.search(title) else 1
    return HeatsinkFacts(fits=sorted(fits), count=count, working=working)
