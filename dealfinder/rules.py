"""Rule reader: listing text in, facts out. Rules only (spec #1, D4)."""
import re
from dataclasses import dataclass

from .config import MIN_DISK_TB

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
_FAMILY_35 = re.compile(rf"{_ENTERPRISE_FAMILY}|{_NAS_FAMILY}|\bpurple\b|\bskyhawk\b|\bbarracuda\b", re.I)
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
    count: int = 1  # disks this one Listing sells as one lot, at its one price ("4x 16TB")

    @property
    def qualifies(self):
        return (self.capacity_tb is not None and self.capacity_tb >= MIN_DISK_TB and self.form_factor == "3.5"
                and self.working and self.genuine)


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
    lot = _DISKS_X.search(title)
    if lot and sizes == {float(lot[2].replace(",", ".")), int(lot[1]) * float(lot[2].replace(",", "."))}:
        sizes = {float(lot[2].replace(",", "."))}
    if not sizes and not _DISK_WORD.search(title):
        return None  # a family word alone is not a disk ("Exos" riflescopes and backpacks)
    if _ACCESSORY.search(title):  # a part or accessory for a disk, not a disk
        return None
    missing = []
    if len(sizes) != 1:
        missing.append("capacity")
    disk_class = _disk_class(title)
    # an external USB enclosure is rejected on class alone; its bay size is irrelevant
    form_factor = "external" if disk_class == "external" else _form_factor(title)
    if form_factor is None:
        missing.append("form_factor")
    if condition not in CONDITIONS:
        missing.append("condition")
    count = _disk_count(title)
    if count == 1 and _QUANTITY.search(title) and not _PER_UNIT.search(title):
        missing.append("quantity")  # names several disks, but not how many the price buys ("lot", "10 stk på lager")
    too_small = bool(sizes) and max(sizes) < MIN_DISK_TB
    # a known disqualifying fact decides it: rejected, not "could not read"
    ruled_out = (too_small or form_factor in ("2.5", "external")
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
        count=count,
    )


# "4x 16TB", "4 x Exos", "4 stk", "lot of 4"; not stock on hand ("10 stk på lager", "har 12 stk"): see _STICK_COUNT
_DISKS_X = re.compile(r"(?<![\w.,])(\d{1,2})\s?[x×*]\s?(\d{1,2}(?:[.,]\d)?)\s?tb\b", re.I)


def _disk_count(title):
    """Disks one Listing sells as a lot: 1 when priced per disk, else the title's count, else 1."""
    m = not _PER_UNIT.search(title) and (_DISKS_X.search(title) or _STICK_COUNT.search(title))
    return max(1, int(next(g for g in m.groups() if g))) if m else 1


_DELL_MODEL = re.compile(r"\b([rt])(\d)(\d)(\d)(\d)?\s?(xd2|xd|xs|xa|xr)?\b", re.I)
_DELL_CONTEXT = re.compile(r"\bdell\b|poweredge|\bidrac", re.I)
_HP_MODEL = re.compile(r"\b(dl|ml)\s?(\d{2,3})[a-z]?\s?(?:gen\s?|g)(\d{1,2})\b", re.I)
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

_STICK_SIZES = {4, 8, 16, 32, 64, 128}  # GB; a "256 GB" alone is a total of unknown sticks, "(2x 480GB)" SSDs
# a speed in MT/s: "DDR4-2400", "2400MHz", "2933MT/s", "2666V", "PC4-2400T", or a PC rating "PC4-19200";
# not a CPU model "E5-2666 v3" or a PSU "1600W"
_SPEED = re.compile(r"(?<![\w.,])(?<!e5-)(\d{4,5})(?=\s?(?:mhz|mt/?s)|[a-z]{0,2}\b)(?!\s?w\b)", re.I)
_PC_RATING = {8500: 1066, 10600: 1333, 12800: 1600, 14900: 1866, 17000: 2133, 19200: 2400, 21300: 2666, 23400: 2933,
              25600: 3200}
_SPEEDS = set(_PC_RATING.values())


def _speed(text):
    """The one DDR speed in MT/s stated in `text`, None when none or several."""
    found = {_PC_RATING.get(n, n) for n in map(int, _SPEED.findall(text))} & _SPEEDS
    return found.pop() if len(found) == 1 else None


_CPU_NAME = re.compile(
    r"(?<![a-wyz])e5[-\s]?(?P<e5>[24][46](?!00)\d\d)(?P<e5s>[lwa]?)(?:\s?v(?P<v>[1-4]))?\b"
    r"|\b(?P<tier>bronze|silver|gold|platinum)\s?(?P<sp>[3-9][1-5]\d\d)(?P<sps>[a-z]{0,2})\b"
    r"|\bepyc\s?(?P<epyc>7(?!00)[\dfhb]{2}[1-3])(?P<epycs>p?)\b", re.I)  # 7302, 7F52, 7H12, 74F3
_CPU_TEXT = re.compile(r"\bxeon\b|\bepyc\b|\b(?:cpus?|prosessor\w*|processors?)", re.I)
_SERVER_TEXT = re.compile(r"thinksystem|primergy|\bucs\b|\bproliant\b", re.I)
_PAIR = re.compile(r"\bpairs?\b|\b\w*par\b", re.I)  # "matchet prosessorpar"


# ponytail: without these words the count is trusted, so a "10x Samsung 32GB" priced per stick reads as 10 sticks;
# the RAM NOK-per-GB floor catches the worst of it
_PER_UNIT = re.compile(r"\bpris\s+(?:per|pr)\b|\bstykkpris\b|\bpr\.?\s*st(?:k|ykk)\b"
                       r"|\bper\s+(?:s[tr]k|stykk|brikke|modul|stick|module|piece)\b|\beach\b", re.I)
# a description is stricter: it also carries spec text ("16GB per modul") and single-unit offers on a lot
# ("enkeltvis for 750kr per brikke"). "Pris (er) per stk", "stykkpris" and "pris pr." always count; an amount in
# NOK on the same line before a bare "per/pr stk|stykk|srk|brikke|modul" counts only when it is the Listing price
_PRICED_UNIT = re.compile(
    r"(?P<pris>\bpris\s+(?:er\s+)?(?:per|pr)\.?\s*(?:s[tr]k|stykk)\b|\bstykkpris\b|\bpris\s+pr\.)"
    r"|(?<![\d.,])(?P<amount>\d{1,3}(?:[ .]\d{3})+|\d+)[ \t]*(?:kr|nok|,-)\.?[ \t]*(?:per|pr)\b\.?[ \t]*"
    r"(?:s[tr]k|stykk|brikke|modul)\b", re.I)


def priced_per_unit(description, price):
    """True when a description says the Listing's price is for one unit (finn.no 471846605: "Pris per srk.")."""
    return any(m["pris"] or int(re.sub(r"\D", "", m["amount"])) == price
               for m in _PRICED_UNIT.finditer(description or ""))


# units on sale in a description that prices one ("Selger 4 stk. Pris per stk", "har 5 stk", "3 disker igjen").
# ponytail: the first count wins, so "2 stk caddies følger med" reads as 2 disks; the owner checks the stated stock before buying
_STOCK = re.compile(r"(?<![\w.,])(\d{1,2})\s?(?:stk|stykk(?:er)?|pcs|pieces|disker|igjen)\b", re.I)


def stock_in(description):
    """Units a description priced per unit says are on sale; 1 when it does not say."""
    m = _STOCK.search(description or "")
    return max(1, int(m[1])) if m else 1


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
    if not _RAM_TEXT.search(title) or _NOT_RAM.search(title) or _CPU_TEXT.search(title) or _CPU_NAME.search(title):
        return None
    if _WORKSTATION.search(title) and not _FOR_SERVER.search(title):
        return None  # a whole workstation; "RAM for Dell Precision T7810" is RAM
    server = (_HP_MODEL.search(title) or _DELL_MODEL.search(title) and _DELL_CONTEXT.search(title)
              or _SUPERMICRO.search(title) and _SERVER_WORD.search(title) or _SERVER_TEXT.search(title))
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
    sticks = 1 if _PER_UNIT.search(title) else sticks
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
