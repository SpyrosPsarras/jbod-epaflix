"""Rule reader: listing text in, facts out. Rules only (spec #1, D4)."""
import re
from dataclasses import dataclass

_FAULTY = re.compile(r"faulty|defekt|defect|\bparts\b|spares|broken|\bdead\b|not\s+working|\bdoa\b", re.I)


@dataclass
class Unreadable:
    missing: list


def _aspect_values(aspects, *names):
    return [str(v) for k, value in (aspects or {}).items() if not names or k.casefold() in names
            for v in (value if isinstance(value, list) else [value]) if v is not None]


def _one(values):
    return next(iter(values)) if len(values) == 1 else None


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
    r"|(?<![\d.,])(?P<amount>\d{1,3}(?:[ .]\d{3})+|\d+)[ \t]*(?:(?:kr|nok|,-)\.?[ \t]*)?(?:per|pr)\b\.?[ \t]*"
    r"(?:s[tr]k|stykk|brikke|modul)\b", re.I)


def priced_per_unit(description, price):
    """True when a description says the Listing's price is for one unit (finn.no 471846605: "Pris per srk.")."""
    return any(m["pris"] or int(re.sub(r"\D", "", m["amount"])) == price
               for m in _PRICED_UNIT.finditer(description or ""))


_RAM_TEXT = re.compile(r"dimm|\bram\b|minne|memory|\bddr[2-5]|\bpc[2-5]l?-|registered|registrert", re.I)
# desktop, laptop, NAS or unbuffered memory, or a whole computer: not a server RAM Listing
_NOT_RAM = re.compile(
    r"so-?dimm|\blaptop|notebook|bærbar|\b(?:200|204|260)-?pin|vengeance|hyperx|\bfury\b|g\.?skill"
    r"|ripjaws|trident|ballistix|dominator|\brgb\b|non[-\s]?ecc|\bi[3579]\b|ryzen|\b\d+\s?tb\b|\b[1-4]u\b|\bnas\b"
    r"|\bidrac|kjerner|\bcores?\b|motherboard|mainboard|hovedkort|\bvrtx\b", re.I)
_WORKSTATION = re.compile(r"workstation|precision|\bz[468]\d0\b", re.I)
_RAM_GB = re.compile(r"(?<![\w.,])(?!(?:13|14)g\b)(\d{1,3})\s?g(?:b|o)?\b(?!\s?/\s?s)", re.I)
# "4x 16GB", "4 x 16GB", or "8GB(X4)"
_STICKS_X = re.compile(r"(?<![\w.,])(\d{1,2})\s?[x×*]\s?(\d{1,3})\s?gb\b"
                       r"|(?<![\w.,])(\d{1,3})\s?gb\s?\(?[x×*]\s?(\d{1,2})\b", re.I)
# "8x Samsung", "8 stk", "4 pcs.", "× 4 st.", "kit of 8", "lot of 4"; not "1x 2Rx4" or "Dual Rank x4", nor stock on hand:
# "(4 pcs available)", "10 stk på lager", "har 12 stk"
_STICK_COUNT = re.compile(
    r"(?<![\w.,/-])(?<!har\s)(\d{1,2})\s?(?:[x×*](?=\s|$)(?!\s?\d)|stk\b|pcs\b|pieces\b|st\.)"
    r"(?!\s*(?:available|tilgjengelig|på\s+lager|ledig))"
    r"|(?<!\w)[x×]\s?(\d{1,2})\s?(?:st|stk|pcs)\b|\b(?:kit|lot|set|sett)\s+(?:of|med|på)\s+(\d{1,2})\b", re.I)
_DDR = re.compile(r"\bddr\s?([2-5])(?!\d)|\bpc([2-5])l?(?:-|\b)", re.I)
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


def _ram_model_types(text):
    found = set()
    for model in re.findall(r"\b(?:m(?:393|386|391|378|471|474)\w*|hmaa?\w+|mta\w+|ct\w+|d4es0[12]-\w+)\b", text, re.I):
        model = model.upper()
        if (model.startswith("M393") or re.match(r"HMAA?\w+R7", model)
                or re.match(r"MTA\w+P(?:D)?Z", model) or re.match(r"CT\d+G4RFD", model)):
            found.add("RDIMM")
        elif model.startswith("M386") or re.match(r"HMAA?\w+L7", model) or re.match(r"MTA\w+LZ", model):
            found.add("LRDIMM")
        elif model.startswith(("M391", "M378", "M471", "M474", "D4ES")) or re.match(r"MTA\w+(?:AZ|HZ)", model):
            found.add("unbuffered")
    return found


def _ram_types(text):
    found = set()
    if re.search(r"\budimm\b|unbuffered|so-?dimm|non[-\s]?ecc|ikke[-\s]?(?:ecc|bufret)", text, re.I):
        return {"unbuffered"}
    if _LRDIMM.search(text):
        found.add("LRDIMM")
    if (_RDIMM.search(text) and not _LRDIMM.search(text)
            or re.search(r"(?<![a-z])rdimm\b", text, re.I)):
        found.add("RDIMM")
    return found


def _ram_size_count(text):
    products = [(int(m[1] or m[4]), int(m[2] or m[3])) for m in _STICKS_X.finditer(text)
                if int(m[2] or m[3]) in _STICK_SIZES and 1 <= int(m[1] or m[4]) <= 32]  # not "32x2 GB"
    n = _STICK_COUNT.search(text)
    counted = bool(products or n or _PAIR.search(text))
    if products:
        sticks = products[0][0]
        sizes = {gb for _, gb in products}
    else:
        sticks = max(1, int(next(g for g in n.groups() if g))) if n else 2 if _PAIR.search(text) else 1
        sizes = set(map(int, _RAM_GB.findall(text)))
        sizes = {s for s in sizes if s * sticks in sizes} or sizes  # "kit of 4 16GB 64GB": 16 per stick
        sizes &= _STICK_SIZES
    counts = {count for count, _ in products}
    return sizes, sticks, counted, len(counts) > 1


def read_ram(title, condition, description=None, aspects=None, price=None):
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
    sizes, sticks, counted, count_conflict = _ram_size_count(title)
    gens = {int(a or b) for a, b in _DDR.findall(title)}
    model_types = _ram_model_types(title)
    types = model_types or _ram_types(title)
    if types == {"unbuffered"}:
        return None
    speed = _speed(title)
    if (not types and sizes == {64} and gens == {4} and speed in (2133, 2400)
            and re.search(r"\b4d?rx4\b", title, re.I)):
        types = {"LRDIMM"}
    conflicts = ["quantity"] if count_conflict else []
    description = description or ""
    per_unit = bool(_PER_UNIT.search(title)) or priced_per_unit(description, price)
    if not counted and (not sizes or not per_unit):
        desc_sizes, desc_sticks, desc_counted, desc_count_conflict = _ram_size_count(description)
        if desc_count_conflict and not per_unit:
            conflicts.append("quantity")
        totals = set(map(int, _RAM_GB.findall(title)))
        if desc_counted and desc_sizes and totals and not per_unit:
            if len(desc_sizes) != 1 or totals != {desc_sticks * _one(desc_sizes)}:
                conflicts.append("gb_per_stick")
            else:
                sizes, sticks, counted = desc_sizes, desc_sticks, True
        elif not totals:
            sizes = desc_sizes
            if len(sizes) > 1:
                conflicts.append("gb_per_stick")
            if desc_counted:
                sticks, counted = desc_sticks, True
    counted = counted or per_unit
    facts_description = re.sub(
        r"\b(?:ikke\s+kompatibel\s+med|not\s+(?:for|compatible\s+with)|passer\s+ikke)\b[^.!?;\n]*"
        r"|\b(?:no|not|ikke|uten)\b(?:[ \t]+[\w-]+){0,3}?[ \t]+"
        r"(?:[ur]dimm|lrdimm|so-?dimm|unbuffered|registered|registrert|reg|load[\s-]?reduced)\b",
        "", description, flags=re.I)
    if not gens:
        gens = {int(a or b) for a, b in _DDR.findall(facts_description)}
        if len(gens) > 1:
            conflicts.append("ddr")
    if not types:
        types = _ram_types(facts_description) or _ram_model_types(facts_description)
        if (len(types) > 1 or re.search(r"\bso-?dimm\b", facts_description, re.I)
                and re.search(r"(?<![\w-])dimm\b", facts_description, re.I)):
            conflicts.append("type")
    if not conflicts and types == {"unbuffered"}:
        return None
    speed = speed or _speed(description)
    if (not types and sizes == {64} and gens == {4} and speed in (2133, 2400)
            and re.search(r"\b4d?rx4\b", title + " " + description, re.I)):
        types = {"LRDIMM"}
    aspect_sizes = set().union(*(set(map(int, _RAM_GB.findall(v))) for v in _aspect_values(
        aspects, "capacity per module", "capacité mémoire")))
    aspect_gens = {int(a or b) for v in _aspect_values(aspects, "type") for a, b in _DDR.findall(v)}
    aspect_types = set().union(*(_ram_types(v) for v in _aspect_values(aspects, "ram form factor", "form factor")))
    if len(sizes) != 1 and aspect_sizes:
        sizes = aspect_sizes & _STICK_SIZES
        if len(aspect_sizes) > 1:
            conflicts.append("gb_per_stick")
    if not gens:
        gens = aspect_gens
        if len(gens) > 1:
            conflicts.append("ddr")
    if not types:
        types = _ram_model_types(" ".join(_aspect_values(aspects, "mpn", "model"))) or aspect_types
        if len(types) > 1:
            conflicts.append("type")
    if types == {"unbuffered"} and not conflicts:
        return None
    if not counted:
        counts = {int(v) for v in _aspect_values(aspects, "number of modules")
                  if v.isascii() and v.isdigit() and 1 <= int(v) <= 32}
        totals = {int(n) for v in _aspect_values(aspects, "total capacity") for n in _RAM_GB.findall(v)}
        gb = _one(sizes)
        if totals and gb:
            if any(total % gb or not 1 <= total // gb <= 32 for total in totals):
                conflicts.append("quantity")
            counts |= {total // gb for total in totals if total % gb == 0 and 1 <= total // gb <= 32}
        if len(counts) > 1:
            conflicts.append("quantity")
        sticks = _one(counts) or sticks
    sticks = 1 if per_unit else sticks
    gb = _one(sizes)
    ddr = min(gens) if gens and 4 not in gens else _one(gens)
    ram_type = _one(types)
    working = condition != "for_parts" and not _FAULTY.search(title)
    missing = [name for name, value in (("gb_per_stick", gb), ("ddr", ddr), ("type", ram_type)) if value is None]
    if conflicts and (not gens or 4 in gens):
        return Unreadable(list(dict.fromkeys(conflicts + missing)))
    # a known disqualifying fact decides it: rejected, not "could not read" (DDR3 is readable, only DDR4 qualifies)
    if missing and working and ddr in (None, 4):
        return Unreadable(missing)
    return RamFacts(gb_per_stick=gb, sticks=sticks, ddr=ddr, type=ram_type, speed=speed,
                    ecc=ram_type is not None or bool(re.search(r"\becc\b", title, re.I)), working=working)
